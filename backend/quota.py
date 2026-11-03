"""Only complete, finite account snapshots can update monetary state."""
import math


def quota_snapshot(profile):
    if not isinstance(profile, dict):
        return None
    values = [profile.get('quota'), profile.get('used_quota')]
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or v < 0 for v in values):
        return None
    quota, used = values
    return {'balance': round(quota / 500000, 2), 'used_quota': round(used / 500000, 2),
            'total': round((quota + used) / 500000, 2)}
