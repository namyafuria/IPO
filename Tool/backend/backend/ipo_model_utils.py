"""
ipo_model_utils.py — shared helper classes for the IPO Analyser bucket
models. The sector target-encoder needs to live in its own importable
module (not inline in a build script) because sklearn pickles a class
by its module path -- if it's defined in __main__ of a build script,
nothing else (predict_by_name.py, another rebuild session) can
unpickle the model. Import this module wherever a *_bucket_model.pkl
is loaded or built.
"""

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin

N_CLASSES = 5


class SectorTargetEncoder(BaseEstimator, TransformerMixin):
    """Bayesian-smoothed per-sector bucket-probability encoding.
    Fit on a training fold's sectors+labels only; unseen sectors at
    transform time fall back to the training fold's global bucket
    frequency. smoothing=10 means a sector needs ~10+ rows before its
    own frequency dominates the global prior -- most sectors in this
    project have well under 10 rows (see §29.1/build script coverage
    checks)."""

    def __init__(self, smoothing=10, n_classes=N_CLASSES):
        self.smoothing = smoothing
        self.n_classes = n_classes

    def fit(self, X, y):
        sector = np.asarray(X).reshape(-1)
        y = np.asarray(y)
        self.global_freq_ = np.bincount(y, minlength=self.n_classes) / len(y)
        stats = {}
        for s in pd.unique(sector):
            mask = sector == s
            n_s = mask.sum()
            counts = np.bincount(y[mask], minlength=self.n_classes)
            freq_s = counts / n_s
            stats[s] = (freq_s * n_s + self.global_freq_ * self.smoothing) / (n_s + self.smoothing)
        self.stats_ = stats
        return self

    def transform(self, X):
        sector = np.asarray(X).reshape(-1)
        out = np.zeros((len(sector), self.n_classes))
        for i, s in enumerate(sector):
            out[i] = self.stats_.get(s, self.global_freq_)
        return out
