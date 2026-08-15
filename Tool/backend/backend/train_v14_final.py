import pickle
import numpy as np
import pandas as pd
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import FunctionTransformer
from ipo_model_utils import SectorTargetEncoder

df = pd.read_pickle('v14_full_df.pkl')  # built below in a preceding step

MB_EDGES = [-np.inf, 0, 10, 30, np.inf]
MB_LABELS = ['Loss', '0-10%', '10-30%', '30%+']
SME_EDGES = [-np.inf, 0, 10, np.inf]
SME_LABELS = ['Loss', '0-10%', '10%+']


def bucket_target(gain, edges):
    return pd.cut(gain, bins=edges, labels=False, include_lowest=True)


# ---------------- Mainboard v14_gmp ----------------
mb = df[(df['issue_category'] == 'Mainboard') & df['gmp_percent'].notna()].copy()
mb['gmp_pct_trend_slope'] = mb['gmp_pct_trend_slope'].fillna(0)
mb['gmp_pct_days_since_last_drop'] = mb['gmp_pct_days_since_last_drop'].fillna(0)
y_mb = bucket_target(mb['listing_day_gain_pct'], MB_EDGES).values

mb_pre = ColumnTransformer(transformers=[
    ('sector', SectorTargetEncoder(n_classes=4), 'sector'),
    ('sub', FunctionTransformer(np.log1p), ['subscription_total']),
    ('gmp', 'passthrough', ['gmp_percent']),
    ('gmp_trend', 'passthrough', ['gmp_pct_trend_slope', 'gmp_pct_days_since_last_drop']),
])
mb_pipe = Pipeline([('pre', mb_pre), ('clf', LogisticRegression(max_iter=1000))])
mb_pipe.fit(mb[['sector', 'subscription_total', 'gmp_percent', 'gmp_pct_trend_slope', 'gmp_pct_days_since_last_drop']], y_mb)

mainboard_v14_gmp = {
    'model': mb_pipe,
    'features': ['sector (smoothed target-encoded, smoothing=10, n_classes=4)',
                 'log1p(subscription_total)', 'gmp_percent',
                 'gmp_pct_trend_slope', 'gmp_pct_days_since_last_drop'],
    'algorithm': 'LogisticRegression',
    'bucket_edges': MB_EDGES,
    'bucket_labels': MB_LABELS,
    'issue_category': 'Mainboard',
    'validated_top_bucket_accuracy': 0.506,
    'validated_naive_top_bucket_accuracy': 0.253,
    'validated_log_loss': 1.128,
    'validated_naive_log_loss': 1.405,
    'n_rolling_splits': 8,
    'n_training_rows': len(mb),
    'calibration_method': 'none',
    'validation_note': (
        'v14_gmp: adds gmp_pct_trend_slope + gmp_pct_days_since_last_drop (from '
        'gmp_trend day-wise data, IPO Ji Stage 3 pipeline) on top of v13_gmp\'s '
        'sector + log1p(subscription_total) + gmp_percent. TimeSeriesSplit(8), '
        'walk-forward re-fit each fold (own reconstruction, not n_rolling_splits '
        'from the original custom rolling-window code -- numbers are close to '
        'v13_gmp\'s validated 46.4%/1.170 but not from an identical harness, so '
        'treat as directionally comparable). Result: 50.6% acc vs 25.3% naive '
        '(v13_gmp-equivalent baseline reconstructed at 49.4%/25.3% on same rows), '
        'log-loss 1.128 vs 1.405 naive (baseline 1.132). Small but consistent '
        'improvement on both metrics. Use when gmp_percent AND gmp_trend day-wise '
        'data are both available; falls back needed for companies without trend data.'
    ),
}
with open('mainboard_bucket_model_v14_gmp.pkl', 'wb') as f:
    pickle.dump(mainboard_v14_gmp, f)
print('Mainboard v14_gmp saved, N =', len(mb))

# ---------------- SME v14_gmp ----------------
sme = df[(df['issue_category'] == 'SME') & df['gmp_percent'].notna()].copy()
sme['gmp_pct_change_1d'] = sme['gmp_pct_change_1d'].fillna(0)
sme['gmp_pct_close_to_listing_delta'] = sme['gmp_pct_close_to_listing_delta'].fillna(0)
sme['log_sub'] = np.log1p(pd.to_numeric(sme['subscription_total'], errors='coerce'))
y_sme = bucket_target(sme['listing_day_gain_pct'], SME_EDGES).values

sme_pre = ColumnTransformer(transformers=[
    ('log_sub', 'passthrough', ['log_sub']),
    ('gmp_percent', 'passthrough', ['gmp_percent']),
    ('gmp_trend', 'passthrough', ['gmp_pct_change_1d', 'gmp_pct_close_to_listing_delta']),
])
sme_pipe = Pipeline([('prep', sme_pre), ('clf', LogisticRegression(max_iter=2000))])
sme_pipe.fit(sme[['log_sub', 'gmp_percent', 'gmp_pct_change_1d', 'gmp_pct_close_to_listing_delta']], y_sme)

sme_v14_gmp = {
    'model': sme_pipe,
    'features': ['log1p(subscription_total)', 'gmp_percent',
                 'gmp_pct_change_1d', 'gmp_pct_close_to_listing_delta'],
    'algorithm': 'LogisticRegression',
    'bucket_edges': SME_EDGES,
    'bucket_labels': SME_LABELS,
    'issue_category': 'SME',
    'validated_top_bucket_accuracy': 0.631,
    'validated_naive_top_bucket_accuracy': 0.503,
    'validated_log_loss': 0.743,
    'validated_naive_log_loss': 1.068,
    'n_rolling_splits': 7,
    'n_training_rows': len(sme),
    'calibration_method': 'none',
    'validation_note': (
        'v14_gmp: adds gmp_pct_change_1d + gmp_pct_close_to_listing_delta (from '
        'gmp_trend day-wise data) on top of v7_gmp\'s log1p(subscription_total) + '
        'gmp_percent. TimeSeriesSplit(7) walk-forward re-fit reconstruction (own '
        'harness, not identical to n_rolling_splits methodology -- close to '
        'v7_gmp\'s validated 65.7%/0.732 but treat as directionally comparable). '
        'Result: 63.1% acc vs 50.3% naive (baseline reconstructed 62.7%/50.3% on '
        'same rows), log-loss 0.743 vs 1.068 naive (baseline 0.743, tied). Small, '
        'real improvement in accuracy; log-loss essentially unchanged.'
    ),
}
with open('sme_bucket_model_v14_gmp.pkl', 'wb') as f:
    pickle.dump(sme_v14_gmp, f)
print('SME v14_gmp saved, N =', len(sme))
