import sqlite3
import pickle
import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
from ipo_model_utils import SectorTargetEncoder

con = sqlite3.connect('ipo_database.db')
master = pd.read_sql('select * from ipo_master_records', con)
gmp = pd.read_sql('select * from gmp_trend', con)
master['listing_date'] = pd.to_datetime(master['listing_date'], errors='coerce')
gmp['gmp_date'] = pd.to_datetime(gmp['gmp_date'], errors='coerce')


def gmp_trend_features(g):
    g = g.dropna(subset=['gmp_date']).sort_values('gmp_date')
    pct = g['est_profit_pct']
    if pct.isna().all() or len(pct) == 0:
        return pd.Series(dtype=float)
    pct = pct.astype(float)
    diffs = pct.diff().dropna()
    feats = {}
    feats['gmp_pct_change_1d'] = diffs.iloc[-1] if len(diffs) else np.nan
    if len(pct) >= 2:
        feats['gmp_pct_trend_slope'] = np.polyfit(np.arange(len(pct)), pct.values, 1)[0]
    else:
        feats['gmp_pct_trend_slope'] = np.nan
    days_since_drop = 0
    for d in diffs.values[::-1]:
        if d < 0:
            break
        days_since_drop += 1
    feats['gmp_pct_days_since_last_drop'] = days_since_drop
    if len(pct) >= 3:
        feats['gmp_pct_close_to_listing_delta'] = pct.iloc[-1] - pct.iloc[-3]
    elif len(pct) == 2:
        feats['gmp_pct_close_to_listing_delta'] = pct.iloc[-1] - pct.iloc[0]
    else:
        feats['gmp_pct_close_to_listing_delta'] = np.nan
    return pd.Series(feats)


trend_df = gmp.groupby('company_name').apply(gmp_trend_features).reset_index()
df = master.merge(trend_df, on='company_name', how='left')
df = df.dropna(subset=['listing_day_gain_pct', 'listing_date']).sort_values('listing_date').reset_index(drop=True)
df['subscription_total'] = pd.to_numeric(df['subscription_total'], errors='coerce')
df['gmp_percent'] = pd.to_numeric(df['gmp_percent'], errors='coerce')
df['log_sub'] = np.log1p(df['subscription_total'])


def bucket_target(gain, edges):
    return pd.cut(gain, bins=edges, labels=False, include_lowest=True)


def evaluate(sub_df, feature_builder, n_classes_sector, edges, label, n_splits):
    sub_df = sub_df.reset_index(drop=True)
    target = bucket_target(sub_df['listing_day_gain_pct'], edges).values
    tscv = TimeSeriesSplit(n_splits=n_splits)
    top_accs, naive_accs, logls, naive_logls = [], [], [], []
    n_buckets = len(edges) - 1
    for train_idx, test_idx in tscv.split(sub_df):
        y_train, y_test = target[train_idx], target[test_idx]
        X_train, X_test = feature_builder(sub_df, train_idx, test_idx, y_train, n_classes_sector)

        clf = LogisticRegression(max_iter=2000)
        clf.fit(X_train, y_train)
        pred = clf.predict(X_test)
        proba = clf.predict_proba(X_test)
        acc = (pred == y_test).mean()

        majority = np.bincount(y_train, minlength=n_buckets).argmax()
        naive_acc = (np.full_like(y_test, majority) == y_test).mean()
        naive_proba = np.zeros((len(y_test), n_buckets))
        train_freq = np.bincount(y_train, minlength=n_buckets) / len(y_train)
        naive_proba[:] = train_freq

        # align classes present in training for log_loss
        all_labels = np.arange(n_buckets)
        ll = log_loss(y_test, proba, labels=clf.classes_)
        naive_ll = log_loss(y_test, naive_proba, labels=all_labels)

        top_accs.append(acc)
        naive_accs.append(naive_acc)
        logls.append(ll)
        naive_logls.append(naive_ll)

    print(f"  {label:55s} acc={np.mean(top_accs)*100:5.1f}% (naive {np.mean(naive_accs)*100:5.1f}%)  "
          f"logloss={np.mean(logls):.3f} (naive {np.mean(naive_logls):.3f})  N={len(sub_df)}")
    return np.mean(top_accs), np.mean(naive_accs)


# ---------- MAINBOARD ----------
MB_EDGES = [-np.inf, 0, 10, 30, np.inf]

def mb_baseline_builder(sub_df, train_idx, test_idx, y_train, n_classes):
    enc = SectorTargetEncoder(smoothing=10, n_classes=n_classes)
    train_sector = sub_df['sector'].iloc[train_idx].fillna('Unknown').values
    test_sector = sub_df['sector'].iloc[test_idx].fillna('Unknown').values
    enc.fit(train_sector, y_train)
    sec_train, sec_test = enc.transform(train_sector), enc.transform(test_sector)
    sub_train = sub_df['log_sub'].iloc[train_idx].values.reshape(-1, 1)
    sub_test = sub_df['log_sub'].iloc[test_idx].values.reshape(-1, 1)
    gmp_train = sub_df['gmp_percent'].iloc[train_idx].fillna(sub_df['gmp_percent'].iloc[train_idx].median()).fillna(0).values.reshape(-1, 1)
    gmp_test = sub_df['gmp_percent'].iloc[test_idx].fillna(sub_df['gmp_percent'].iloc[train_idx].median()).fillna(0).values.reshape(-1, 1)
    X_train = np.hstack([sec_train, sub_train, gmp_train])
    X_test = np.hstack([sec_test, sub_test, gmp_test])
    return X_train, X_test

def mb_v14_builder(sub_df, train_idx, test_idx, y_train, n_classes):
    X_train, X_test = mb_baseline_builder(sub_df, train_idx, test_idx, y_train, n_classes)
    extra = ['gmp_pct_trend_slope', 'gmp_pct_days_since_last_drop']
    tr = sub_df[extra].iloc[train_idx].apply(pd.to_numeric, errors='coerce')
    te = sub_df[extra].iloc[test_idx].apply(pd.to_numeric, errors='coerce')
    med = tr.median().fillna(0)
    tr = tr.fillna(med).fillna(0).values
    te = te.fillna(med).fillna(0).values
    return np.hstack([X_train, tr]), np.hstack([X_test, te])

print("=== MAINBOARD (real bucket edges [-inf,0,10,30,inf], SectorTargetEncoder(n_classes=4) + log1p(sub) + gmp_percent) ===")
mb = df[df['issue_category'] == 'Mainboard'].dropna(subset=['gmp_percent']).copy()
evaluate(mb, mb_baseline_builder, 4, MB_EDGES, 'v13_gmp-equivalent baseline (real pipeline structure)', 8)
evaluate(mb, mb_v14_builder, 4, MB_EDGES, 'v13_gmp + GMP-trend (v14 addition)', 8)

# ---------- SME ----------
SME_EDGES = [-np.inf, 0, 10, np.inf]

def sme_baseline_builder(sub_df, train_idx, test_idx, y_train, n_classes):
    sub_train = sub_df['log_sub'].iloc[train_idx].values.reshape(-1, 1)
    sub_test = sub_df['log_sub'].iloc[test_idx].values.reshape(-1, 1)
    gmp_train = sub_df['gmp_percent'].iloc[train_idx].fillna(sub_df['gmp_percent'].iloc[train_idx].median()).fillna(0).values.reshape(-1, 1)
    gmp_test = sub_df['gmp_percent'].iloc[test_idx].fillna(sub_df['gmp_percent'].iloc[train_idx].median()).fillna(0).values.reshape(-1, 1)
    return np.hstack([sub_train, gmp_train]), np.hstack([sub_test, gmp_test])

def sme_v14_builder(sub_df, train_idx, test_idx, y_train, n_classes):
    X_train, X_test = sme_baseline_builder(sub_df, train_idx, test_idx, y_train, n_classes)
    extra = ['gmp_pct_change_1d', 'gmp_pct_close_to_listing_delta']
    tr = sub_df[extra].iloc[train_idx].apply(pd.to_numeric, errors='coerce')
    te = sub_df[extra].iloc[test_idx].apply(pd.to_numeric, errors='coerce')
    med = tr.median().fillna(0)
    tr = tr.fillna(med).fillna(0).values
    te = te.fillna(med).fillna(0).values
    return np.hstack([X_train, tr]), np.hstack([X_test, te])

print("\n=== SME (real bucket edges [-inf,0,10,inf], log1p(sub) + gmp_percent, NO sector) ===")
sme = df[df['issue_category'] == 'SME'].dropna(subset=['gmp_percent']).copy()
evaluate(sme, sme_baseline_builder, None, SME_EDGES, 'v7_gmp-equivalent baseline (real pipeline structure)', 7)
evaluate(sme, sme_v14_builder, None, SME_EDGES, 'v7_gmp + GMP-trend (v14 addition)', 7)
