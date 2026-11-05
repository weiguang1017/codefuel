"""Only complete, finite account snapshots can update monetary state."""
import math


def quota_snapshot(profile, quota_per_unit=500000):
    if (not isinstance(profile, dict) or isinstance(quota_per_unit, bool)
            or not isinstance(quota_per_unit, (int, float)) or not math.isfinite(quota_per_unit) or quota_per_unit <= 0):
        return None
    values = [profile.get('quota'), profile.get('used_quota')]
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v < 0 for v in values):
        return None
    quota, used = values
    return {'balance': round(quota / quota_per_unit, 2), 'used_quota': round(used / quota_per_unit, 2),
            'total': round((quota + used) / quota_per_unit, 2)}
