# Portions of this file are adapted from livepeer-python-gateway (src/livepeer_gateway/live_runner.py,
# ``_runner_payment_type`` and ``_pad_runner_price``, at 2017a80),
# https://github.com/livepeer/livepeer-python-gateway, under this license:
#
# MIT License
#
# Copyright (c) 2026 Livepeer Foundation
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""What the engine pays a runner: the payment type for its price unit, and the signer's ceiling (D62).

Adapted from livepeer-python-gateway ``_runner_payment_type`` and ``_pad_runner_price`` at ``2017a80``
(MIT; the notice above).
"""

from decimal import Decimal
from typing import Any

from livepeer_builder.contracts import NetworkRate
from livepeer_builder.errors import RunnerCallError
from livepeer_builder.utils import PAYMENT_TYPES_BY_UNIT, signable

_PRICE_PADDING = Decimal("1.012")  # the signer refuses a ceiling below the price it is asked to pay


def payment_type(rate: NetworkRate) -> str:
    unit = rate.unit
    if unit not in PAYMENT_TYPES_BY_UNIT:
        expected = ", ".join(sorted(PAYMENT_TYPES_BY_UNIT))
        message = f"unsupported runner price unit {unit!r}; expected {expected}"
        raise RunnerCallError("payment_refused", message, payment_sent=False)
    return PAYMENT_TYPES_BY_UNIT[unit]


def max_price(rate: NetworkRate | None) -> dict[str, Any] | None:
    """The signer's ceiling, or None when there is none to give: no rate, a free one, or a currency and unit
    the signer refuses every ceiling in."""
    if rate is None or not signable(rate.amount, rate.currency, rate.unit):
        return None
    return {
        "price": float(rate.amount * _PRICE_PADDING),
        "currency": rate.currency,
        "unit": rate.unit,
    }


def no_ceiling(rate: NetworkRate | None) -> str:
    if rate is None:
        return "no usable price"
    if rate.amount == 0:
        return "a price of 0"
    return f"a price in {rate.currency} per {rate.unit}, which the signer takes no ceiling in"
