import json
import os

import joblib
import numpy as np

MODELS_DIR = "models"
STATS_DIR = "stats"
MODEL_PATH = os.path.join(MODELS_DIR, "nba_model.pkl")
FEATURE_IMPORTANCE_PATH = os.path.join(STATS_DIR, "feature_importance.json")

FEATURE_NAMES = [
    # YES-team stats
    "t1_win_pct", "t1_wins", "t1_losses",
    "t1_pts", "t1_opp_pts", "t1_pts_differential",
    "t1_reb", "t1_ast", "t1_stl", "t1_blk", "t1_tov",
    "t1_fg_pct", "t1_fg3_pct", "t1_ft_pct",
    "t1_off_rtg", "t1_def_rtg", "t1_net_rtg", "t1_pace",
    "t1_home_pct", "t1_away_pct", "t1_last10_pct", "t1_streak",
    # NO-team stats
    "t2_win_pct", "t2_wins", "t2_losses",
    "t2_pts", "t2_opp_pts", "t2_pts_differential",
    "t2_reb", "t2_ast", "t2_stl", "t2_blk", "t2_tov",
    "t2_fg_pct", "t2_fg3_pct", "t2_ft_pct",
    "t2_off_rtg", "t2_def_rtg", "t2_net_rtg", "t2_pace",
    "t2_home_pct", "t2_away_pct", "t2_last10_pct", "t2_streak",
    # Matchup differentials
    "win_pct_diff", "pts_diff", "pts_against_diff",
    "net_rtg_diff", "off_rtg_diff", "pace_diff",
    "stl_diff", "blk_diff", "tov_diff",
    # Player features
    "t1_top3_avg_pts", "t2_top3_avg_pts",
    "t1_top_scorer_pts", "t2_top_scorer_pts",
    "t1_total_stl", "t2_total_stl",
    "t1_total_blk", "t2_total_blk",
    "player_pts_diff",
    # Market context
    "kalshi_yes_price", "market_line", "our_win_prob", "edge",
]

_WINNER_TYPES = {"winner", "2h_winner", "series_spread"}
_TOTAL_TYPES = {"total", "team_total"}
_SPREAD_TYPES = {"spread"}

_models: dict = {}


def _load_models() -> dict:
    if not os.path.exists(MODEL_PATH):
        return {}
    try:
        return joblib.load(MODEL_PATH)
    except Exception:
        return {}


def _to_vec(features: dict) -> np.ndarray:
    return np.array([features.get(k, 0.0) for k in FEATURE_NAMES], dtype=float).reshape(1, -1)


def _baseline_winner(features: dict) -> dict:
    diff = features.get("win_pct_diff", 0.0)
    net_diff = features.get("net_rtg_diff", 0.0)
    score = diff * 0.6 + net_diff * 0.025
    last10_diff = features.get("t1_last10_pct", 0.5) - features.get("t2_last10_pct", 0.5)
    score += last10_diff * 0.15

    confidence = min(95, 50 + abs(score) * 80)
    prediction = "YES" if score >= 0 else "NO"
    reasoning = (
        f"Win% diff {features.get('win_pct_diff', 0):+.3f}, "
        f"NetRtg diff {features.get('net_rtg_diff', 0):+.1f}"
    )
    return {"prediction": prediction, "confidence": round(confidence, 1), "reasoning": reasoning, "method": "baseline"}


def _baseline_total(features: dict, kalshi_line: float | None) -> dict:
    pace_factor = features.get("pace_avg", 100.0) / 100.0
    projected = (features.get("t1_ppg", 110.0) + features.get("t2_ppg", 110.0)) * pace_factor * 0.5
    if kalshi_line is None:
        return {
            "prediction": "YES",
            "confidence": 52.0,
            "reasoning": f"Projected total {projected:.1f} (no line available)",
            "method": "baseline",
            "predicted_value": round(projected, 1),
        }
    prediction = "YES" if projected >= kalshi_line else "NO"
    diff = abs(projected - kalshi_line)
    confidence = min(90, 50 + diff * 2)
    reasoning = f"Projected {projected:.1f} vs line {kalshi_line:.1f}"
    return {
        "prediction": prediction,
        "confidence": round(confidence, 1),
        "reasoning": reasoning,
        "method": "baseline",
        "predicted_value": round(projected, 1),
    }


def _baseline_spread(features: dict, kalshi_line: float | None) -> dict:
    pace_factor = features.get("pace_avg", 100.0) / 100.0
    net_diff = features.get("net_rtg_diff", 0.0)
    projected_margin = net_diff * pace_factor * 0.4
    if kalshi_line is None:
        prediction = "YES" if projected_margin >= 0 else "NO"
        return {
            "prediction": prediction,
            "confidence": 52.0,
            "reasoning": f"Projected margin {projected_margin:+.1f} (no line available)",
            "method": "baseline",
            "predicted_value": round(projected_margin, 1),
        }
    prediction = "YES" if projected_margin >= kalshi_line else "NO"
    diff = abs(projected_margin - kalshi_line)
    confidence = min(88, 50 + diff * 1.5)
    reasoning = f"Projected margin {projected_margin:+.1f} vs line {kalshi_line:+.1f}"
    return {
        "prediction": prediction,
        "confidence": round(confidence, 1),
        "reasoning": reasoning,
        "method": "baseline",
        "predicted_value": round(projected_margin, 1),
    }


def predict(features: dict, market_type: str) -> dict:
    global _models
    if not _models:
        _models = _load_models()

    kalshi_line = features.get("kalshi_line")

    if market_type in _WINNER_TYPES:
        clf = _models.get("winner")
        if clf is not None:
            try:
                vec = _to_vec(features)
                proba = clf.predict_proba(vec)[0]
                yes_prob = proba[1] if len(proba) > 1 else proba[0]
                confidence = min(95, max(51, yes_prob * 100))
                prediction = "YES" if yes_prob >= 0.5 else "NO"
                return {
                    "prediction": prediction,
                    "confidence": round(confidence, 1),
                    "reasoning": f"ML model: {yes_prob:.1%} YES probability",
                    "method": "ml_model",
                }
            except Exception:
                pass
        return _baseline_winner(features)

    if market_type in _TOTAL_TYPES:
        reg = _models.get("total")
        if reg is not None:
            try:
                vec = _to_vec(features)
                predicted_value = float(reg.predict(vec)[0])
                if kalshi_line is not None:
                    prediction = "YES" if predicted_value >= kalshi_line else "NO"
                    diff = abs(predicted_value - kalshi_line)
                    confidence = min(90, 50 + diff * 2)
                    return {
                        "prediction": prediction,
                        "confidence": round(confidence, 1),
                        "reasoning": f"ML model: predicted {predicted_value:.1f} vs line {kalshi_line:.1f}",
                        "method": "ml_model",
                        "predicted_value": round(predicted_value, 1),
                    }
            except Exception:
                pass
        return _baseline_total(features, kalshi_line)

    if market_type in _SPREAD_TYPES:
        reg = _models.get("spread")
        if reg is not None:
            try:
                vec = _to_vec(features)
                predicted_value = float(reg.predict(vec)[0])
                if kalshi_line is not None:
                    prediction = "YES" if predicted_value >= kalshi_line else "NO"
                    diff = abs(predicted_value - kalshi_line)
                    confidence = min(88, 50 + diff * 1.5)
                    return {
                        "prediction": prediction,
                        "confidence": round(confidence, 1),
                        "reasoning": f"ML model: predicted margin {predicted_value:+.1f} vs line {kalshi_line:+.1f}",
                        "method": "ml_model",
                        "predicted_value": round(predicted_value, 1),
                    }
            except Exception:
                pass
        return _baseline_spread(features, kalshi_line)

    return _baseline_winner(features)


def _log_feature_importance(model_name: str, model, n_top: int = 10) -> list[dict]:
    importances = model.feature_importances_
    ranked = sorted(
        zip(FEATURE_NAMES, importances),
        key=lambda x: x[1],
        reverse=True,
    )
    print(f"  [{model_name}] top {n_top} features:")
    for feat, imp in ranked[:n_top]:
        print(f"    {feat:<28} {imp:.4f}")
    return [{"feature": f, "importance": round(float(imp), 6)} for f, imp in ranked]


def _save_feature_importance(importance_by_model: dict) -> None:
    os.makedirs(STATS_DIR, exist_ok=True)
    from datetime import datetime, timezone
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "feature_count": len(FEATURE_NAMES),
        "models": importance_by_model,
    }
    with open(FEATURE_IMPORTANCE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"  feature importance saved → {FEATURE_IMPORTANCE_PATH}")


def train(training_data: list[dict]) -> bool:
    try:
        from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
        from sklearn.model_selection import cross_val_score

        winner_X, winner_y = [], []
        total_X, total_y = [], []
        spread_X, spread_y = [], []

        for entry in training_data:
            feats = entry.get("features", {})
            if not feats:
                continue
            vec = [feats.get(k, 0.0) for k in FEATURE_NAMES]
            mtype = entry.get("market_type", "")
            outcome = entry.get("outcome")

            if mtype in _WINNER_TYPES and outcome in (0, 1):
                winner_X.append(vec)
                winner_y.append(outcome)
            elif mtype in _TOTAL_TYPES and outcome is not None:
                total_X.append(vec)
                total_y.append(float(outcome))
            elif mtype in _SPREAD_TYPES and outcome is not None:
                spread_X.append(vec)
                spread_y.append(float(outcome))

        os.makedirs(MODELS_DIR, exist_ok=True)
        trained = {}
        importance_by_model = {}

        if len(winner_X) >= 10:
            clf = GradientBoostingClassifier(n_estimators=100, random_state=42)
            if len(winner_X) >= 50:
                cv_scores = cross_val_score(clf, winner_X, winner_y, cv=5, scoring="accuracy")
                print(f"  winner CV accuracy: {cv_scores.mean():.3f} ± {cv_scores.std():.3f}")
            clf.fit(winner_X, winner_y)
            trained["winner"] = clf
            importance_by_model["winner"] = _log_feature_importance("winner", clf)
            print(f"  trained winner classifier on {len(winner_X)} samples")

        if len(total_X) >= 10:
            reg = GradientBoostingRegressor(n_estimators=100, random_state=42)
            if len(total_X) >= 50:
                cv_scores = cross_val_score(reg, total_X, total_y, cv=5, scoring="r2")
                print(f"  total CV R²: {cv_scores.mean():.3f} ± {cv_scores.std():.3f}")
            reg.fit(total_X, total_y)
            trained["total"] = reg
            importance_by_model["total"] = _log_feature_importance("total", reg)
            print(f"  trained total regressor on {len(total_X)} samples")

        if len(spread_X) >= 10:
            reg = GradientBoostingRegressor(n_estimators=100, random_state=42)
            if len(spread_X) >= 50:
                cv_scores = cross_val_score(reg, spread_X, spread_y, cv=5, scoring="r2")
                print(f"  spread CV R²: {cv_scores.mean():.3f} ± {cv_scores.std():.3f}")
            reg.fit(spread_X, spread_y)
            trained["spread"] = reg
            importance_by_model["spread"] = _log_feature_importance("spread", reg)
            print(f"  trained spread regressor on {len(spread_X)} samples")

        if trained:
            joblib.dump(trained, MODEL_PATH)
            if importance_by_model:
                _save_feature_importance(importance_by_model)
            global _models
            _models = trained
            return True

    except Exception as exc:
        print(f"  train error: {exc}")

    return False
