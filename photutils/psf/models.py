# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
This module provides models for doing PSF/PRF-fitting photometry.
"""

import copy
import itertools
import warnings
from functools import lru_cache

import numpy as np
from astropy.modeling import Fittable2DModel, Parameter
from astropy.nddata import NDData
from astropy.utils.exceptions import AstropyWarning

from photutils.aperture import CircularAperture
from photutils.utils._parameters import as_pair

__all__ = ['NonNormalizable', 'FittableImageModel', 'EPSFModel',
           'GriddedPSFModel', 'IntegratedGaussianPRF', 'PRFAdapter']


class NonNormalizable(AstropyWarning):
    """
    Used to indicate that a :py:class:`FittableImageModel` model is
    non-normalizable.
    """


class FittableImageModel(Fittable2DModel):
    r"""
    A fittable 2D model of an image allowing for image intensity scaling
    and image translations.

    This class takes 2D image data and computes the values of the model
    at arbitrary locations (including at intra-pixel, fractional
    positions) within this image using spline interpolation provided by
    :py:class:`~scipy.interpolate.RectBivariateSpline`.

    The fittable model provided by this class has three model
    parameters: an image intensity scaling factor (``flux``) which is
    applied to (normalized) image, and two positional parameters
    (``x_0`` and ``y_0``) indicating the location of a feature in the
    coordinate grid on which the model is to be evaluated.

    If this class is initialized with ``flux`` (intensity scaling
    factor) set to `None`, then ``flux`` is going to be estimated as
    ``sum(data)``.

    Parameters
    ----------
    data : `~numpy.ndarray`
        Array containing 2D image.

    origin : tuple, None, optional
        A reference point in the input image ``data`` array. When origin
        is `None`, origin will be set at the middle of the image array.

        If ``origin`` represents the location of a feature (e.g., the
        position of an intensity peak) in the input ``data``, then model
        parameters ``x_0`` and ``y_0`` show the location of this peak in
        an another target image to which this model was fitted.
        Fundamentally, it is the coordinate in the model's image data
        that should map to coordinate (``x_0``, ``y_0``) of the output
        coordinate system on which the model is evaluated.

        Alternatively, when ``origin`` is set to ``(0, 0)``, then model
        parameters ``x_0`` and ``y_0`` are shifts by which model's image
        should be translated in order to match a target image.

    normalize : bool, optional
        Indicates whether or not the model should be build on normalized
        input image data. If true, then the normalization constant (*N*)
        is computed so that

        .. math::
            N \cdot C \cdot \sum\limits_{i,j} D_{i,j} = 1,

        where *N* is the normalization constant, *C* is correction
        factor given by the parameter ``normalization_correction``, and
        :math:`D_{i,j}` are the elements of the input image ``data``
        array.

    normalization_correction : float, optional
        A strictly positive number that represents correction that needs
        to be applied to model's data normalization (see *C* in the
        equation in the comments to ``normalize`` for more details).  A
        possible application for this parameter is to account for
        aperture correction. Assuming model's data represent a PSF to be
        fitted to some target star, we set ``normalization_correction``
        to the aperture correction that needs to be applied to the
        model. That is, ``normalization_correction`` in this case should
        be set to the ratio between the total flux of the PSF (including
        flux outside model's data) to the flux of model's data.  Then,
        best fitted value of the ``flux`` model parameter will represent
        an aperture-corrected flux of the target star.  In the case of
        aperture correction, ``normalization_correction`` should be a
        value larger than one, as the total flux, including regions
        outside of the aperture, should be larger than the flux inside
        the aperture, and thus the correction is applied as an inversely
        multiplied factor.

    fill_value : float, optional
        The value to be returned by the `evaluate` or
        ``astropy.modeling.Model.__call__`` methods when evaluation is
        performed outside the definition domain of the model.

    kwargs : dict, optional
        Additional optional keyword arguments to be passed directly to
        the `compute_interpolator` method.  See `compute_interpolator`
        for more details.

    oversampling : int or array_like (int)
        The integer oversampling factor(s) of the ePSF relative to the
        input ``stars`` along each axis. If ``oversampling`` is a scalar
        then it will be used for both axes. If ``oversampling`` has two
        elements, they must be in ``(y, x)`` order.
    """

    flux = Parameter(description='Intensity scaling factor for image data.',
                     default=1.0)
    x_0 = Parameter(description='X-position of a feature in the image in '
                    'the output coordinate grid on which the model is '
                    'evaluated.', default=0.0)
    y_0 = Parameter(description='Y-position of a feature in the image in '
                    'the output coordinate grid on which the model is '
                    'evaluated.', default=0.0)

    def __init__(self, data, *, flux=flux.default, x_0=x_0.default,
                 y_0=y_0.default, normalize=False,
                 normalization_correction=1.0, origin=None, oversampling=1,
                 fill_value=0.0, **kwargs):

        self._fill_value = fill_value
        self._img_norm = None
        self._normalization_status = 0 if normalize else 2
        self._store_interpolator_kwargs(**kwargs)
        self._oversampling = as_pair('oversampling', oversampling,
                                     lower_bound=(0, 1))

        if normalization_correction <= 0:
            raise ValueError("'normalization_correction' must be strictly "
                             "positive.")
        self._normalization_correction = normalization_correction
        self._normalization_constant = 1.0 / self._normalization_correction

        self._data = np.array(data, copy=True, dtype=float)

        if not np.all(np.isfinite(self._data)):
            raise ValueError("All elements of input 'data' must be finite.")

        # set input image related parameters:
        self._ny, self._nx = self._data.shape
        self._shape = self._data.shape
        if self._data.size < 1:
            raise ValueError("Image data array cannot be zero-sized.")

        # set the origin of the coordinate system in image's pixel grid:
        self.origin = origin

        flux = self._initial_norm(flux, normalize)

        super().__init__(flux, x_0, y_0)

        # initialize interpolator:
        self.compute_interpolator(**kwargs)

    def _initial_norm(self, flux, normalize):

        if flux is None:
            if self._img_norm is None:
                self._img_norm = self._compute_raw_image_norm()
            flux = self._img_norm

        self._compute_normalization(normalize)

        return flux

    def _compute_raw_image_norm(self):
        """
        Helper function that computes the uncorrected inverse
        normalization factor of input image data. This quantity is
        computed as the *sum of all pixel values*.

        .. note::
            This function is intended to be overridden in a subclass if
            one desires to change the way the normalization factor is
            computed.
        """
        return np.sum(self._data, dtype=float)

    def _compute_normalization(self, normalize=True):
        r"""
        Helper function that computes (corrected) normalization factor
        of the original image data. This quantity is computed as the
        inverse "raw image norm" (or total "flux" of model's image)
        corrected by the ``normalization_correction``:

        .. math::
            N = 1/(\Phi * C),

        where :math:`\Phi` is the "total flux" of model's image as
        computed by `_compute_raw_image_norm` and *C* is the
        normalization correction factor. :math:`\Phi` is computed only
        once if it has not been previously computed. Otherwise, the
        existing (stored) value of :math:`\Phi` is not modified as
        :py:class:`FittableImageModel` does not allow image data to be
        modified after the object is created.

        .. note::
            Normally, this function should not be called by the
            end-user. It is intended to be overridden in a subclass if
            one desires to change the way the normalization factor is
            computed.
        """
        self._normalization_constant = 1.0 / self._normalization_correction

        if normalize:
            # compute normalization constant so that
            # N*C*sum(data) = 1:
            if self._img_norm is None:
                self._img_norm = self._compute_raw_image_norm()

            if self._img_norm != 0.0 and np.isfinite(self._img_norm):
                self._normalization_constant /= self._img_norm
                self._normalization_status = 0

            else:
                self._normalization_constant = 1.0
                self._normalization_status = 1
                warnings.warn("Overflow encountered while computing "
                              "normalization constant. Normalization "
                              "constant will be set to 1.", NonNormalizable)

        else:
            self._normalization_status = 2

    @property
    def oversampling(self):
        """
        The factor by which the stored image is oversampled.

        An input to this model is multiplied by this factor to yield the
        index into the stored image.
        """
        return self._oversampling

    @property
    def data(self):
        """Get original image data."""
        return self._data

    @property
    def normalized_data(self):
        """Get normalized and/or intensity-corrected image data."""
        return self._normalization_constant * self._data

    @property
    def normalization_constant(self):
        """Get normalization constant."""
        return self._normalization_constant

    @property
    def normalization_status(self):
        """
        Get normalization status. Possible status values are:

        - 0: **Performed**. Model has been successfully normalized at
             user's request.
        - 1: **Failed**. Attempt to normalize has failed.
        - 2: **NotRequested**. User did not request model to be normalized.
        """
        return self._normalization_status

    @property
    def normalization_correction(self):
        """
        Set/Get flux correction factor.

        .. note::
            When setting correction factor, model's flux will be
            adjusted accordingly such that if this model was a good fit
            to some target image before, then it will remain a good fit
            after correction factor change.
        """
        return self._normalization_correction

    @normalization_correction.setter
    def normalization_correction(self, normalization_correction):
        old_cf = self._normalization_correction
        self._normalization_correction = normalization_correction
        self._compute_normalization(normalize=self._normalization_status != 2)

        # adjust model's flux so that if this model was a good fit to
        # some target image, then it will remain a good fit after
        # correction factor change:
        self.flux *= normalization_correction / old_cf

    @property
    def shape(self):
        """
        A tuple of dimensions of the data array in numpy style (ny, nx).
        """
        return self._shape

    @property
    def nx(self):
        """Number of columns in the data array."""
        return self._nx

    @property
    def ny(self):
        """Number of rows in the data array."""
        return self._ny

    @property
    def origin(self):
        """
        A tuple of ``x`` and ``y`` coordinates of the origin of the
        coordinate system in terms of pixels of model's image.

        When setting the coordinate system origin, a tuple of two `int`
        or `float` may be used. If origin is set to `None`, the origin
        of the coordinate system will be set to the middle of the data
        array (``(npix-1)/2.0``).

        .. warning::
            Modifying ``origin`` will not adjust (modify) model's
            parameters ``x_0`` and ``y_0``.
        """
        return (self._x_origin, self._y_origin)

    @origin.setter
    def origin(self, origin):
        if origin is None:
            self._x_origin = (self._nx - 1) / 2.0
            self._y_origin = (self._ny - 1) / 2.0
        elif hasattr(origin, '__iter__') and len(origin) == 2:
            self._x_origin, self._y_origin = origin
        else:
            raise TypeError("Parameter 'origin' must be either None or an "
                            "iterable with two elements.")

    @property
    def x_origin(self):
        """X-coordinate of the origin of the coordinate system."""
        return self._x_origin

    @property
    def y_origin(self):
        """Y-coordinate of the origin of the coordinate system."""
        return self._y_origin

    @property
    def fill_value(self):
        """
        Fill value to be returned for coordinates outside of the domain
        of definition of the interpolator. If ``fill_value`` is `None`,
        then values outside of the domain of definition are the ones
        returned by the interpolator.
        """
        return self._fill_value

    @fill_value.setter
    def fill_value(self, fill_value):
        self._fill_value = fill_value

    def _store_interpolator_kwargs(self, **kwargs):
        """
        This function should be called in a subclass whenever model's
        interpolator is (re-)computed.
        """
        self._interpolator_kwargs = copy.deepcopy(kwargs)

    @property
    def interpolator_kwargs(self):
        """
        Get current interpolator's arguments used when interpolator was
        created.
        """
        return self._interpolator_kwargs

    def compute_interpolator(self, **kwargs):
        """
        Compute/define the interpolating spline.

        This function can be overridden in a subclass to define custom
        interpolators.

        Parameters
        ----------
        **kwargs : dict, optional
            Additional optional keyword arguments:

            - **degree** : int, tuple, optional
                Degree of the interpolating spline. A tuple can be used
                to provide different degrees for the X- and Y-axes.
                Default value is degree=3.

            - **s** : float, optional
                Non-negative smoothing factor. Default value s=0
                corresponds to interpolation.  See
                :py:class:`~scipy.interpolate.RectBivariateSpline` for
                more details.

        Notes
        -----
        * When subclassing :py:class:`FittableImageModel` for the
          purpose of overriding :py:func:`compute_interpolator`, the
          :py:func:`evaluate` may need to overridden as well depending
          on the behavior of the new interpolator. In addition, for
          improved future compatibility, make sure that the overriding
          method stores keyword arguments ``kwargs`` by calling
          ``_store_interpolator_kwargs`` method.

        * Use caution when modifying interpolator's degree or smoothness
          in a computationally intensive part of the code as it may
          decrease code performance due to the need to recompute
          interpolator.
        """
        from scipy.interpolate import RectBivariateSpline

        if 'degree' in kwargs:
            degree = kwargs['degree']
            if hasattr(degree, '__iter__') and len(degree) == 2:
                degx = int(degree[0])
                degy = int(degree[1])
            else:
                degx = int(degree)
                degy = int(degree)
            if degx < 0 or degy < 0:
                raise ValueError("Interpolator degree must be a non-negative "
                                 "integer")
        else:
            degx = 3
            degy = 3

        smoothness = kwargs.get('s', 0)

        x = np.arange(self._nx, dtype=float)
        y = np.arange(self._ny, dtype=float)
        self.interpolator = RectBivariateSpline(
            x, y, self._data.T, kx=degx, ky=degy, s=smoothness
        )

        self._store_interpolator_kwargs(**kwargs)

    def evaluate(self, x, y, flux, x_0, y_0, *, use_oversampling=True):
        """
        Evaluate the model on some input variables and provided model
        parameters.

        Parameters
        ----------
        use_oversampling : bool, optional
            Whether to use the oversampling factor to calculate the
            model pixel indices. The default is `True`, which means the
            input indices will be multiplied by this factor.
        """
        if use_oversampling:
            xi = self._oversampling[1] * (np.asarray(x) - x_0)
            yi = self._oversampling[0] * (np.asarray(y) - y_0)
        else:
            xi = np.asarray(x) - x_0
            yi = np.asarray(y) - y_0

        xi = xi.astype(float)
        yi = yi.astype(float)
        xi += self._x_origin
        yi += self._y_origin

        f = flux * self._normalization_constant
        evaluated_model = f * self.interpolator.ev(xi, yi)

        if self._fill_value is not None:
            # find indices of pixels that are outside the input pixel grid and
            # set these pixels to the 'fill_value':
            invalid = (((xi < 0) | (xi > self._nx - 1))
                       | ((yi < 0) | (yi > self._ny - 1)))
            evaluated_model[invalid] = self._fill_value

        return evaluated_model


class EPSFModel(FittableImageModel):
    """
    A class that models an effective PSF (ePSF).

    The EPSFModel is normalized such that the sum of the PSF over the
    (undersampled) pixels within the the input ``norm_radius`` is 1.0.
    This means that when the EPSF is fit to stars, the resulting flux
    corresponds to aperture photometry within a circular aperture of
    radius ``norm_radius``.

    While this class is a subclass of `FittableImageModel`, it is very
    similar.  The primary differences/motivation are a few additional
    parameters necessary specifically for ePSFs.

    Parameters
    ----------
    oversampling : int or array_like (int)
        The integer oversampling factor(s) of the ePSF relative to the
        input ``stars`` along each axis. If ``oversampling`` is a scalar
        then it will be used for both axes. If ``oversampling`` has two
        elements, they must be in ``(y, x)`` order.

    norm_radius : float, optional
        The radius inside which the ePSF is normalized by the sum over
        undersampled integer pixel values inside a circular aperture.
    """

    def __init__(self, data, *, flux=1.0, x_0=0.0, y_0=0.0, normalize=True,
                 normalization_correction=1.0, origin=None, oversampling=1,
                 fill_value=0.0, norm_radius=5.5, **kwargs):

        self._norm_radius = norm_radius

        super().__init__(data=data, flux=flux, x_0=x_0, y_0=y_0,
                         normalize=normalize,
                         normalization_correction=normalization_correction,
                         origin=origin, oversampling=oversampling,
                         fill_value=fill_value, **kwargs)

    def _initial_norm(self, flux, normalize):
        if flux is None:
            if self._img_norm is None:
                self._img_norm = self._compute_raw_image_norm()
            flux = self._img_norm

        if normalize:
            self._compute_normalization()
        else:
            self._img_norm = self._compute_raw_image_norm()

        return flux

    def _compute_raw_image_norm(self):
        """
        Compute the normalization of input image data as the flux
        within a given radius.
        """
        xypos = (self._nx / 2.0, self._ny / 2.0)
        # TODO: generalize "radius" (ellipse?) is oversampling is
        # different along x/y axes
        radius = self._norm_radius * self.oversampling[0]
        aper = CircularAperture(xypos, r=radius)
        flux, _ = aper.do_photometry(self._data, method='exact')
        return flux[0] / np.prod(self.oversampling)

    def _compute_normalization(self, normalize=True):
        """
        Helper function that computes (corrected) normalization factor
        of the original image data. For the ePSF this is defined as the
        sum over the inner N (default=5.5) pixels of the non-oversampled
        image. Will re-normalize the data to the value calculated.
        """
        if normalize:
            if self._img_norm is None:
                if np.sum(self._data) == 0:
                    self._img_norm = 1
                else:
                    self._img_norm = self._compute_raw_image_norm()

            if self._img_norm != 0.0 and np.isfinite(self._img_norm):
                self._data /= (self._img_norm * self._normalization_correction)
                self._normalization_status = 0
            else:
                self._normalization_status = 1
                self._img_norm = 1
                warnings.warn('Overflow encountered while computing '
                              'normalization constant. Normalization '
                              'constant will be set to 1.', NonNormalizable)
        else:
            self._normalization_status = 2

    @property
    def normalized_data(self):
        """
        Overloaded dummy function that also returns self._data, as the
        normalization occurs within _compute_normalization in EPSFModel,
        and as such self._data will sum, accounting for
        under/oversampled pixels, to 1/self._normalization_correction.
        """
        return self._data

    @FittableImageModel.origin.setter
    def origin(self, origin):
        if origin is None:
            self._x_origin = (self._nx - 1) / 2.0 / self.oversampling[1]
            self._y_origin = (self._ny - 1) / 2.0 / self.oversampling[0]
        elif (hasattr(origin, '__iter__') and len(origin) == 2):
            self._x_origin, self._y_origin = origin
        else:
            raise TypeError("Parameter 'origin' must be either None or an "
                            "iterable with two elements.")

    def compute_interpolator(self, **kwargs):
        """
        Compute/define the interpolating spline.

        This function can be overridden in a subclass to define custom
        interpolators.

        Parameters
        ----------
        **kwargs : dict, optional
            Additional optional keyword arguments:

            - **degree** : int, tuple, optional
                Degree of the interpolating spline. A tuple can be used
                to provide different degrees for the X- and Y-axes.
                Default value is degree=3.

            - **s** : float, optional
                Non-negative smoothing factor. Default value s=0
                corresponds to interpolation.  See
                :py:class:`~scipy.interpolate.RectBivariateSpline` for
                more details.

        Notes
        -----
        * When subclassing :py:class:`FittableImageModel` for the
          purpose of overriding :py:func:`compute_interpolator`, the
          :py:func:`evaluate` may need to overridden as well depending
          on the behavior of the new interpolator. In addition, for
          improved future compatibility, make sure that the overriding
          method stores keyword arguments ``kwargs`` by calling
          ``_store_interpolator_kwargs`` method.

        * Use caution when modifying interpolator's degree or smoothness
          in a computationally intensive part of the code as it may
          decrease code performance due to the need to recompute
          interpolator.
        """
        from scipy.interpolate import RectBivariateSpline

        if 'degree' in kwargs:
            degree = kwargs['degree']
            if hasattr(degree, '__iter__') and len(degree) == 2:
                degx = int(degree[0])
                degy = int(degree[1])
            else:
                degx = int(degree)
                degy = int(degree)
            if degx < 0 or degy < 0:
                raise ValueError("Interpolator degree must be a non-negative "
                                 "integer")
        else:
            degx = 3
            degy = 3

        smoothness = kwargs.get('s', 0)

        # Interpolator must be set to interpolate on the undersampled
        # pixel grid, going from 0 to len(undersampled_grid)
        x = np.arange(self._nx, dtype=float) / self.oversampling[1]
        y = np.arange(self._ny, dtype=float) / self.oversampling[0]
        self.interpolator = RectBivariateSpline(
            x, y, self._data.T, kx=degx, ky=degy, s=smoothness)

        self._store_interpolator_kwargs(**kwargs)

    def evaluate(self, x, y, flux, x_0, y_0):
        """
        Evaluate the model on some input variables and provided model
        parameters.
        """
        xi = np.asarray(x) - x_0 + self._x_origin
        yi = np.asarray(y) - y_0 + self._y_origin

        evaluated_model = flux * self.interpolator.ev(xi, yi)

        if self._fill_value is not None:
            # find indices of pixels that are outside the input pixel
            # grid and set these pixels to the 'fill_value':
            invalid = (((xi < 0) | (xi > (self._nx - 1)
                                    / self.oversampling[1]))
                       | ((yi < 0) | (yi > (self._ny - 1)
                                      / self.oversampling[0])))
            evaluated_model[invalid] = self._fill_value

        return evaluated_model


class GriddedPSFModel(Fittable2DModel):
    """
    A fittable 2D model containing a grid PSF models defined at specific
    locations that are interpolated to evaluate a PSF at an arbitrary
    (x, y) position.

    Parameters
    ----------
    data : `~astropy.nddata.NDData`
        A `~astropy.nddata.NDData` object containing the grid of
        reference PSF arrays. The data attribute must contain a 3D
        `~numpy.ndarray` containing a stack of the 2D PSFs with a shape
        of ``(N_psf, PSF_ny, PSF_nx)``. The meta attribute must be
        `dict` containing the following:

            * ``'grid_xypos'``: A list of the (x, y) grid positions of
              each reference PSF. The order of positions should match the
              first axis of the 3D `~numpy.ndarray` of PSFs. In other
              words, ``grid_xypos[i]`` should be the (x, y) position of
              the reference PSF defined in ``data[i]``.

            * ``'oversampling'``: The integer oversampling factor of the
              PSF.

        The meta attribute may contain other properties such as the
        telescope, instrument, detector, and filter of the PSF.
    """

    flux = Parameter(description='Intensity scaling factor for the PSF '
                     'model.', default=1.0)
    x_0 = Parameter(description='x position in the output coordinate grid '
                    'where the model is evaluated.', default=0.0)
    y_0 = Parameter(description='y position in the output coordinate grid '
                    'where the model is evaluated.', default=0.0)

    def __init__(self, data, *, flux=flux.default, x_0=x_0.default,
                 y_0=y_0.default, fill_value=0.0):

        self._data_input = self._validate_data(data)
        self.data = data.data
        self._meta = data.meta  # use _meta to avoid the meta descriptor
        self.grid_xypos = data.meta['grid_xypos']
        self.oversampling = data.meta['oversampling']
        self.fill_value = fill_value

        self._grid_xpos, self._grid_ypos = np.transpose(self.grid_xypos)
        self._xgrid = np.unique(self._grid_xpos)  # also sorts values
        self._ygrid = np.unique(self._grid_ypos)  # also sorts values
        if (len(list(itertools.product(self._xgrid, self._ygrid)))
                != len(self.grid_xypos)):
            raise ValueError('"grid_xypos" must form a regular grid.')

        self._xidx = np.arange(self.data.shape[2], dtype=float)
        self._yidx = np.arange(self.data.shape[1], dtype=float)

        # Here we avoid decorating the instance method with @lru_cache
        # to prevent memory leaks; we set maxsize=128 to prevent the
        # cache from growing too large.
        self._calc_interpolator = lru_cache(maxsize=128)(
            self._calc_interpolator_uncached)

        super().__init__(flux, x_0, y_0)

    @staticmethod
    def _validate_data(data):
        if not isinstance(data, NDData):
            raise TypeError('data must be an NDData instance.')

        if data.data.ndim != 3:
            raise ValueError('The NDData data attribute must be a 3D numpy '
                             'ndarray')

        if 'grid_xypos' not in data.meta:
            raise ValueError('"grid_xypos" must be in the nddata meta '
                             'dictionary.')
        if len(data.meta['grid_xypos']) != data.data.shape[0]:
            raise ValueError('The length of grid_xypos must match the number '
                             'of input PSFs.')

        if 'oversampling' not in data.meta:
            raise ValueError('"oversampling" must be in the nddata meta '
                             'dictionary.')
        if not np.isscalar(data.meta['oversampling']):
            raise ValueError('oversampling must be a scalar value')

        return data

    def copy(self):
        """
        Return a copy of this model.

        Note that the PSF grid data is not copied. Use the `deepcopy`
        method if you want to copy the PSF grid data.
        """
        return self.__class__(self._data_input, flux=self.flux.value,
                              x_0=self.x_0.value, y_0=self.y_0.value,
                              fill_value=self.fill_value)

    def deepcopy(self):
        """
        Return a deep copy of this model.
        """
        return copy.deepcopy(self)

    def clear_cache(self):
        """
        Clear the internal cache.
        """
        self._calc_interpolator.cache_clear()

    def _cache_info(self):
        """
        Return information about the internal cache.
        """
        return self._calc_interpolator.cache_info()

    @staticmethod
    def _find_start_idx(data, x):
        """
        Find the index of the lower bound where ``x`` should be inserted
        into ``a`` to maintain order.

        The index of the upper bound is the index of the lower bound
        plus 2.  Both bound indices must be within the array.

        Parameters
        ----------
        data : 1D `~numpy.ndarray`
            The 1D array to search.

        x : float
            The value to insert.

        Returns
        -------
        index : int
            The index of the lower bound.
        """
        idx = np.searchsorted(data, x)
        if idx == 0:
            idx0 = 0
        elif idx == len(data):  # pragma: no cover
            idx0 = idx - 2
        else:
            idx0 = idx - 1
        return idx0

    def _find_bounding_points(self, x, y):
        """
        Find the indices of the grid points that bound the input
        ``(x, y)`` position.

        Parameters
        ----------
        x, y : float
            The ``(x, y)`` position where the PSF is to be evaluated.
            The position must be inside the region defined by the grid
            of PSF positions.

        Returns
        -------
        indices : list of int
            A list of indices of the bounding grid points.
        """
        x0 = self._find_start_idx(self._xgrid, x)
        y0 = self._find_start_idx(self._ygrid, y)
        xypoints = list(itertools.product(self._xgrid[x0:x0 + 2],
                                          self._ygrid[y0:y0 + 2]))

        # find the grid_xypos indices of the reference xypoints
        indices = []
        for xx, yy in xypoints:
            indices.append(np.argsort(np.hypot(self._grid_xpos - xx,
                                               self._grid_ypos - yy))[0])

        return indices

    @staticmethod
    def _bilinear_interp(xyref, zref, xi, yi):
        """
        Perform bilinear interpolation of four 2D arrays located at
        points on a regular grid.

        Parameters
        ----------
        xyref : list of 4 (x, y) pairs
            A list of 4 ``(x, y)`` pairs that form a rectangle.

        zref : 3D `~numpy.ndarray`
            A 3D `~numpy.ndarray` of shape ``(4, nx, ny)``. The first
            axis corresponds to ``xyref``, i.e., ``refdata[0, :, :]`` is
            the 2D array located at ``xyref[0]``.

        xi, yi : float
            The ``(xi, yi)`` point at which to perform the
            interpolation.  The ``(xi, yi)`` point must lie within the
            rectangle defined by ``xyref``.

        Returns
        -------
        result : 2D `~numpy.ndarray`
            The 2D interpolated array.
        """
        xyref = [tuple(i) for i in xyref]
        idx = sorted(range(len(xyref)), key=xyref.__getitem__)
        xyref = sorted(xyref)  # sort by x, then y
        (x0, y0), (_x0, y1), (x1, _y0), (_x1, _y1) = xyref

        if x0 != _x0 or x1 != _x1 or y0 != _y0 or y1 != _y1:
            raise ValueError('The refxy points do not form a rectangle.')

        if not np.isscalar(xi):
            xi = xi[0]
        if not np.isscalar(yi):
            yi = yi[0]

        if not x0 <= xi <= x1 or not y0 <= yi <= y1:
            raise ValueError('The (x, y) input is not within the rectangle '
                             'defined by xyref.')

        data = np.asarray(zref)[idx]
        weights = np.array([(x1 - xi) * (y1 - yi), (x1 - xi) * (yi - y0),
                            (xi - x0) * (y1 - yi), (xi - x0) * (yi - y0)])
        norm = (x1 - x0) * (y1 - y0)

        return np.sum(data * weights[:, None, None], axis=0) / norm

    def _calc_interpolator_uncached(self, x_0, y_0):
        """
        Return the local interpolation function for the PSF model at
        (x_0, y_0).

        Note that the interpolator will be cached by _calc_interpolator.
        It can be cleared by calling the clear_cache method.
        """
        from scipy.interpolate import RectBivariateSpline

        if (x_0 < self._xgrid[0] or x_0 > self._xgrid[-1]
                or y_0 < self._ygrid[0] or y_0 > self._ygrid[-1]):
            # position is outside of the grid, so simply use the
            # closest reference PSF
            ref_index = np.argsort(np.hypot(self._grid_xpos - x_0,
                                            self._grid_ypos - y_0))[0]
            psf_image = self.data[ref_index, :, :]
        else:
            # find the four bounding reference PSFs and interpolate
            ref_indices = self._find_bounding_points(x_0, y_0)
            xyref = np.array(self.grid_xypos)[ref_indices]
            psfs = self.data[ref_indices, :, :]

            psf_image = self._bilinear_interp(xyref, psfs, x_0, y_0)

        interpolator = RectBivariateSpline(self._xidx, self._yidx,
                                           psf_image.T, kx=3, ky=3, s=0)

        return interpolator

    def evaluate(self, x, y, flux, x_0, y_0):
        """
        Evaluate the `GriddedPSFModel` for the input parameters.
        """
        # NOTE: the astropy base Model.__call__() method converts scalar
        # inputs to size-1 arrays before calling evaluate().
        if not np.isscalar(flux):
            flux = flux[0]
        if not np.isscalar(x_0):
            x_0 = x_0[0]
        if not np.isscalar(y_0):
            y_0 = y_0[0]

        # Calculate the local interpolation function for the PSF at
        # (x_0, y_0). Only the integer part of the position is input in
        # order to have effective caching.
        interpolator = self._calc_interpolator(int(x_0), int(y_0))

        # now evaluate the PSF at the (x_0, y_0) subpixel position on
        # the input (x, y) values
        xi = self.oversampling * (np.asarray(x, dtype=float) - x_0)
        yi = self.oversampling * (np.asarray(y, dtype=float) - y_0)

        # define origin at the PSF image center
        ny, nx = self.data.shape[1:]
        xi += (nx - 1) / 2
        yi += (ny - 1) / 2

        evaluated_model = flux * interpolator.ev(xi, yi)

        if self.fill_value is not None:
            # find indices of pixels that are outside the input pixel
            # grid and set these pixels to the fill_value
            invalid = (((xi < 0) | (xi > nx - 1))
                       | ((yi < 0) | (yi > ny - 1)))
            evaluated_model[invalid] = self.fill_value

        return evaluated_model


class IntegratedGaussianPRF(Fittable2DModel):
    r"""
    Circular Gaussian model integrated over pixels.

    Because it is integrated, this model is considered a PRF, *not* a
    PSF (see :ref:`psf-terminology` for more about the terminology used
    here.)

    This model is a Gaussian *integrated* over an area of
    ``1`` (in units of the model input coordinates, e.g., 1
    pixel). This is in contrast to the apparently similar
    `astropy.modeling.functional_models.Gaussian2D`, which is the value
    of a 2D Gaussian *at* the input coordinates, with no integration.
    So this model is equivalent to assuming the PSF is Gaussian at a
    *sub-pixel* level.

    Parameters
    ----------
    sigma : float
        Width of the Gaussian PSF.
    flux : float, optional
        Total integrated flux over the entire PSF
    x_0 : float, optional
        Position of the peak in x direction.
    y_0 : float, optional
        Position of the peak in y direction.

    Notes
    -----
    This model is evaluated according to the following formula:

        .. math::

            f(x, y) =
                \frac{F}{4}
                \left[
                {\rm erf} \left(\frac{x - x_0 + 0.5}
                {\sqrt{2} \sigma} \right) -
                {\rm erf} \left(\frac{x - x_0 - 0.5}
                {\sqrt{2} \sigma} \right)
                \right]
                \left[
                {\rm erf} \left(\frac{y - y_0 + 0.5}
                {\sqrt{2} \sigma} \right) -
                {\rm erf} \left(\frac{y - y_0 - 0.5}
                {\sqrt{2} \sigma} \right)
                \right]

    where ``erf`` denotes the error function and ``F`` the total
    integrated flux.
    """

    flux = Parameter(default=1)
    x_0 = Parameter(default=0)
    y_0 = Parameter(default=0)
    sigma = Parameter(default=1, fixed=True)

    _erf = None

    @property
    def bounding_box(self):
        halfwidth = 4 * self.sigma
        return ((int(self.y_0 - halfwidth), int(self.y_0 + halfwidth)),
                (int(self.x_0 - halfwidth), int(self.x_0 + halfwidth)))

    def __init__(self, sigma=sigma.default,
                 x_0=x_0.default, y_0=y_0.default, flux=flux.default,
                 **kwargs):
        if self._erf is None:
            from scipy.special import erf
            self.__class__._erf = erf

        super().__init__(n_models=1, sigma=sigma, x_0=x_0, y_0=y_0, flux=flux,
                         **kwargs)

    def evaluate(self, x, y, flux, x_0, y_0, sigma):
        """Model function Gaussian PSF model."""
        return (flux / 4
                * ((self._erf((x - x_0 + 0.5) / (np.sqrt(2) * sigma))
                    - self._erf((x - x_0 - 0.5) / (np.sqrt(2) * sigma)))
                   * (self._erf((y - y_0 + 0.5) / (np.sqrt(2) * sigma))
                      - self._erf((y - y_0 - 0.5) / (np.sqrt(2) * sigma)))))


class PRFAdapter(Fittable2DModel):
    """
    A model that adapts a supplied PSF model to act as a PRF.

    It integrates the PSF model over pixel "boxes".  A critical built-in
    assumption is that the PSF model scale and location parameters are
    in *pixel* units.

    Parameters
    ----------
    psfmodel : a 2D model
        The model to assume as representative of the PSF
    renormalize_psf : bool
        If True, the model will be integrated from -inf to inf and
        re-scaled so that the total integrates to 1.  Note that this
        renormalization only occurs *once*, so if the total flux of
        ``psfmodel`` depends on position, this will *not* be correct.
    xname : str or None
        The name of the ``psfmodel`` parameter that corresponds to the
        x-axis center of the PSF.  If None, the model will be assumed to
        be centered at x=0.
    yname : str or None
        The name of the ``psfmodel`` parameter that corresponds to the
        y-axis center of the PSF.  If None, the model will be assumed to
        be centered at y=0.
    fluxname : str or None
        The name of the ``psfmodel`` parameter that corresponds to the
        total flux of the star.  If None, a scaling factor will be
        applied by the ``PRFAdapter`` instead of modifying the
        ``psfmodel``.

    Notes
    -----
    This current implementation of this class (using numerical
    integration for each pixel) is extremely slow, and only suited for
    experimentation over relatively few small regions.
    """

    flux = Parameter(default=1)
    x_0 = Parameter(default=0)
    y_0 = Parameter(default=0)

    def __init__(self, psfmodel, *, renormalize_psf=True, flux=flux.default,
                 x_0=x_0.default, y_0=y_0.default, xname=None, yname=None,
                 fluxname=None, **kwargs):

        self.psfmodel = psfmodel.copy()

        if renormalize_psf:
            from scipy.integrate import dblquad
            self._psf_scale_factor = 1.0 / dblquad(self.psfmodel,
                                                   -np.inf, np.inf,
                                                   lambda x: -np.inf,
                                                   lambda x: np.inf)[0]
        else:
            self._psf_scale_factor = 1

        self.xname = xname
        self.yname = yname
        self.fluxname = fluxname

        # these can be used to adjust the integration behavior. Might be
        # used in the future to expose how the integration happens
        self._dblquadkwargs = {}

        super().__init__(n_models=1, x_0=x_0, y_0=y_0, flux=flux, **kwargs)

    def evaluate(self, x, y, flux, x_0, y_0):
        """The evaluation function for PRFAdapter."""
        if not np.isscalar(flux):
            flux = flux[0]
        if not np.isscalar(x_0):
            x_0 = x_0[0]
        if not np.isscalar(y_0):
            y_0 = y_0[0]

        if self.xname is None:
            dx = x - x_0
        else:
            dx = x
            setattr(self.psfmodel, self.xname, x_0)

        if self.xname is None:
            dy = y - y_0
        else:
            dy = y
            setattr(self.psfmodel, self.yname, y_0)

        if self.fluxname is None:
            return (flux * self._psf_scale_factor
                    * self._integrated_psfmodel(dx, dy))
        else:
            setattr(self.psfmodel, self.yname, flux * self._psf_scale_factor)
            return self._integrated_psfmodel(dx, dy)

    def _integrated_psfmodel(self, dx, dy):
        from scipy.integrate import dblquad

        # infer type/shape from the PSF model.  Seems wasteful, but the
        # integration step is a *lot* more expensive so its just peanuts
        out = np.empty_like(self.psfmodel(dx, dy))
        outravel = out.ravel()
        for i, (xi, yi) in enumerate(zip(dx.ravel(), dy.ravel())):
            outravel[i] = dblquad(self.psfmodel,
                                  xi - 0.5, xi + 0.5,
                                  lambda x: yi - 0.5, lambda x: yi + 0.5,
                                  **self._dblquadkwargs)[0]
        return out
