from pydantic import BaseModel


class SalaryInput(BaseModel):
    base_monthly_salary: float
    guaranteed_block_hours: float = 75.0          # hours/month covered by base
    hourly_rate_above_guarantee: float | None = None  # per-hour rate for extra flying
    average_block_hours_flown: float = 75.0
    per_diem_daily: float = 0.0
    per_diem_days_per_month: float = 0.0
    other_monthly_allowances: float = 0.0
    currency: str = "USD"


def calculate(inp: SalaryInput):
    hourly_rate = inp.hourly_rate_above_guarantee
    if hourly_rate is None and inp.guaranteed_block_hours > 0:
        hourly_rate = inp.base_monthly_salary / inp.guaranteed_block_hours

    extra_hours = max(0.0, inp.average_block_hours_flown - inp.guaranteed_block_hours)
    extra_pay = extra_hours * (hourly_rate or 0)

    per_diem_monthly = inp.per_diem_daily * inp.per_diem_days_per_month

    monthly_total = (
        inp.base_monthly_salary + extra_pay + per_diem_monthly + inp.other_monthly_allowances
    )
    annual_total = monthly_total * 12
    annual_block_hours = inp.average_block_hours_flown * 12
    effective_hourly = monthly_total / inp.average_block_hours_flown if inp.average_block_hours_flown else 0

    return {
        "currency": inp.currency,
        "effective_hourly_rate": round(effective_hourly, 2),
        "extra_hours_this_month": round(extra_hours, 2),
        "extra_pay_this_month": round(extra_pay, 2),
        "per_diem_monthly": round(per_diem_monthly, 2),
        "monthly_total": round(monthly_total, 2),
        "annual_total_estimate": round(annual_total, 2),
        "annual_block_hours_estimate": round(annual_block_hours, 2),
    }
