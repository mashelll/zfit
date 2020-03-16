import numpy as np
from scipy import optimize
from ..param import set_values
from ..util.container import convert_to_container


def pll(minimizer, loss, params, values) -> float:
    """Compute minimum profile likelihood for given parameters and values."""
    params = convert_to_container(params)
    values = convert_to_container(values)

    verbosity = minimizer.verbosity
    minimizer.verbosity = 0

    with set_values(params, values):
        for param in params:
            param.floating = False
        minimum = minimizer.minimize(loss=loss)

    for param in params:
        param.floating = True
    minimizer.verbosity = verbosity

    return minimum.fmin


def set_params_to_result(params, result):
    """Set parameters values to the values in the fitted value in the result."""
    for param in params:
        param.set_value(result.params[param]["value"])


def get_crossing_value(result, params, direction, sigma, rootf, rtol):

    all_params = list(result.params.keys())
    loss = result.loss
    errordef = loss.errordef
    fmin = result.fmin
    minimizer = result.minimizer.copy()
    # if "strategy" in minimizer.minimizer_options:
        # With the `Minuit` minimizer. The decrease of the strategy increases the speed
        # of the profile likelihood scan
        # minimizer.minimizer_options["strategy"] = max(0, minimizer.minimizer_options["strategy"] - 1)
    minimizer.tolerance = minimizer.tolerance * 0.5
    rtol *= errordef

    set_params_to_result(all_params, result)

    covariance = result.covariance(as_dict=True)
    sigma = sigma * direction

    to_return = {}
    for param in params:
        param_error = result.hesse(params=param)[param]["error"]
        param_value = result.params[param]["value"]
        exp_root = param_value + sigma * param_error  # expected root

        for ap in all_params:
            if ap == param:
                continue

            # shift parameters, other than param, using covariance matrix
            ap_value = result.params[ap]["value"]
            ap_error = covariance[(ap, ap)] ** 0.5
            ap_value += sigma ** 2 * covariance[(param, ap)] / ap_error
            ap.set_value(ap_value)

        cache = {}
        def shifted_pll(v):
            """
            Computes the pll, with the minimum substracted and shifted by minus the `errordef`, for a
            given parameter.
            `errordef` = 1 for a chisquare fit, = 0.5 for a likelihood fit.
            """
            if v not in cache:
                # shift parameters, other than param, using covariance matrix
                cache[v] = pll(minimizer, loss, param, v) - fmin - errordef

            return cache[v]

        exp_shifted_pll = shifted_pll(exp_root)

        def linear_interp(y):
            """
            Linear interpolation between the minimum of the `shifted_pll` curve and its expected root,
            assuming it is a parabolic curve.
            """
            slope = (exp_root - param_value) / (exp_shifted_pll + errordef)
            return param_value + (y + errordef) * slope
        bound_interp = linear_interp(0)

        if exp_shifted_pll > 0.:
            lower_bound = exp_root
            upper_bound = bound_interp
        else:
            lower_bound = bound_interp
            upper_bound = exp_root

        if direction == 1:
            lower_bound, upper_bound = upper_bound, lower_bound

        # Check if the `shifted_pll` function has the same sign at the lower and upper bounds.
        # If they have the same sign, the window given to the root finding algorithm is increased.
        nsigma = 1.5
        while np.sign(shifted_pll(lower_bound)) == np.sign(shifted_pll(upper_bound)):
            if direction == -1:
                if np.sign(shifted_pll(lower_bound)) == -1:
                    lower_bound = param_value - nsigma * param_error
                else:
                    upper_bound = param_value
            else:
                if np.sign(shifted_pll(lower_bound)) == -1:
                    upper_bound = param_value - nsigma * param_error
                else:
                    lower_bound = param_value

            nsigma += 0.5

        root, results = rootf(f=shifted_pll, a=lower_bound, b=upper_bound, rtol=rtol, full_output=True)

        to_return[param] = root

    return to_return


def _rootf(**kwargs):
    return optimize.toms748(k=1, **kwargs)

# def _rootf(**kwargs):
#     return optimize.brentq(**kwargs)


def compute_errors(result, params, sigma=1, rootf=_rootf, rtol=0.01):
    """
    Compute asymmetric errors of parameters by profiling the loss function in the fit result.

    Args:
        result (`FitResult`): fit result
        params (list(:py:class:`~zfit.Parameter`)): The parameters to calculate the
            errors error. If None, use all parameters.
        sigma (float): Errors are calculated with respect to `sigma` std deviations.
        rootf (callable): function used to find the roots of the loss function
        rtol (float, default=0.01): relative tolerance between the computed and the exact roots

    Returns:
        `OrderedDict`: A `OrderedDict` containing as keys the parameter and as value a `dict` which
            contains two keys 'lower' and 'upper', holding the calculated errors.
            Example: result[par1]['upper'] -> the asymmetric upper error of 'par1'
    """

    params = convert_to_container(params)

    upper_values = get_crossing_value(result=result, params=params, direction=1, sigma=sigma,
                                      rootf=rootf, rtol=rtol)

    lower_values = get_crossing_value(result=result, params=params, direction=-1, sigma=sigma,
                                      rootf=rootf, rtol=rtol)

    to_return = {}
    for param in params:
        fitted_value = result.params[param]["value"]
        to_return[param] = {"lower": lower_values[param] - fitted_value,
                            "upper": upper_values[param] - fitted_value}

    return to_return
