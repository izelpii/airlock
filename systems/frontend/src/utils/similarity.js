// ─── Face similarity presentation helpers ─────────────────────────────────────
//
// The recognition pipeline stores raw *distances* where a SMALLER number means a
// closer match. That is the correct academic measure, but it reads backwards in
// the UI, so every user-facing surface shows a *similarity score* from 0 to 100
// instead, where a LARGER number means a closer match.
//
//     similarity = (1 − distance / scale) × 100,  clamped to 0–100
//
// For cosine (scale 1.0) this is exactly cosine similarity expressed as a
// percentage, since cosine_similarity = 1 − cosine_distance.
//
// Euclidean was removed as a selectable metric. For L2-normalised embeddings it
// is not a second opinion, it is the same number in different units:
//
//     ||a-b||^2 = 2(1 - cos_sim) = 2 * cosine_distance
//     euclidean_l2 = sqrt(2 * cosine_distance)
//
// sqrt is strictly increasing, so euclidean can never rank two candidates
// differently from cosine - it can only print a different score for the same
// pair, which is worse than useless. Measured over the Family catalog, the mean
// |euclidean_l2 - sqrt(2 * cosine)| was 0.000000.
//
// Reference points for cosine:
//     distance 0.00  → 100   (identical)
//     distance 0.40  →  60   (DeepFace's VGG-Face match threshold)
//     distance 1.00  →   0   (unrelated)
//
// This module is presentation-only. Distances remain distances in the database,
// in the workflows, and over the API; nothing here changes what is stored.

/** Upper bound of each metric's practical distance range. */
export const METRIC_SCALE = { cosine: 1.0, contextual: 1.0 };

export const getMetricScale = (metric) => METRIC_SCALE[metric] ?? METRIC_SCALE.cosine;

/**
 * The distance at or below which a candidate is worth showing.
 *
 * This was 0.68, DeepFace's tuned VGG-Face value. That number is not wrong, it
 * was answering a different question: 0.68 is a VERIFICATION threshold - given
 * these two faces, are they the same person - on a balanced pair set. What this
 * page does is IDENTIFICATION: take the nearest of N people. The nearest of 16
 * candidates is a minimum over 16 draws, so its distance is far smaller by
 * construction, and a verification bar applied to it accepts almost anything.
 *
 * Measured on the Family catalog (documentation/tests/bench_threshold.py), with
 * each person's faces held out so the correct answer is "reject":
 *
 *   distance   recall   precision   strangers confidently mislabelled
 *     0.30      94.0%      99.6%       2.4%
 *     0.35      95.1%      99.6%       3.1%   <- here
 *     0.40      95.4%      98.9%      10.8%
 *     0.50      96.5%      98.9%      53.0%
 *     0.68      98.9%      98.9%      99.7%   <- the old default
 *
 * At 0.68, essentially every face of someone not in the library was still
 * labelled as somebody. 0.35 gives up 3.8 points of recall - recoverable by
 * dragging the slider down - and stops 96% of that.
 */
export const METRIC_MATCH_DISTANCE = {
  // Mutable, and seeded with the VGG-Face value measured above. applyEmbeddingModel()
  // replaces it with whatever the catalog's own model reports from
  // GET /api/catalogs/<id>/embedding-model. Until that lands - and for any
  // catalog that has not migrated - this is the right number, because
  // VGG-Face is what an unmigrated catalog embeds with.
  cosine: 0.35,
  // The contextual metric stores 1 - p, so its natural bar is p = 0.5 -> 0.50,
  // i.e. "more likely this person than not". Because the score IS a probability,
  // the displayed 0-100 number reads directly as a confidence percentage.
  contextual: 0.50,
};

/**
 * The match threshold expressed on the 0–100 similarity scale: cosine -> 32.
 * Use this as the natural "show me plausible matches" floor.
 */
/** Human label for what the match bar means, which differs per metric. */
export const matchSimilarityLabel = (metric = 'cosine') =>
  metric === 'contextual'
    ? `${matchSimilarity(metric)}+ means more likely than not`
    : `${matchSimilarity(metric)}+ is a confident match`;

export const matchSimilarity = (metric = 'cosine') =>
  Math.round(
    (1 - (METRIC_MATCH_DISTANCE[metric] ?? METRIC_MATCH_DISTANCE.cosine) / getMetricScale(metric)) * 100
  );

// ─── Which model produced the distances ───────────────────────────────────────
//
// The bar above was measured for VGG-Face, and nothing in this file used to say
// so. A catalog that moved to another model would have gone on being judged by
// the old model's threshold, in a file nobody would think to look in. So the
// active model is now state, set from the server, and `provisional` is carried
// through to the UI rather than hidden: a derived threshold is not a measured
// one and the page should not imply otherwise.
let activeModel = { name: 'VGG-Face', dims: 4096, provisional: false };

/** Called once per catalog, from CatalogContext, with the API's response. */
export const applyEmbeddingModel = (info) => {
  if (!info || !info.name) return activeModel;
  activeModel = {
    name: info.name,
    dims: info.dims,
    provisional: info.match_distance_measured === false,
  };
  if (typeof info.match_distance === 'number') {
    METRIC_MATCH_DISTANCE.cosine = info.match_distance;
  }
  return activeModel;
};

/** The active model, for a UI that wants to name it or warn about it. */
export const embeddingModel = () => activeModel;

/**
 * Default minimum similarity, for the review pages' sliders.
 *
 * A function, not a constant, because it depends on the active model and a
 * constant would have been evaluated at import time - before the catalog, and
 * therefore its model, was known.
 */
export const defaultMinSimilarity = () => matchSimilarity('cosine');

/**
 * Default minimum similarity for the review pages.
 *
 * Derived from METRIC_MATCH_DISTANCE so the slider's starting point and the
 * "confident match" label can never disagree. See that constant for why the bar
 * is an identification threshold measured on this library rather than DeepFace's
 * verification threshold.
 */
/** @deprecated Use defaultMinSimilarity(); this is frozen at import time. */
export const DEFAULT_MIN_SIMILARITY = matchSimilarity('cosine');   // 65

/** Raw distance → 0–100 similarity score (higher = more similar). Null-safe. */
export const distanceToSimilarity = (distance, metric = 'cosine') => {
  if (distance == null || Number.isNaN(Number(distance))) return null;
  const pct = (1 - Number(distance) / getMetricScale(metric)) * 100;
  return Math.max(0, Math.min(100, pct));
};

/** 0–100 similarity score → raw distance. Inverse of distanceToSimilarity. */
export const similarityToDistance = (similarity, metric = 'cosine') =>
  (1 - similarity / 100) * getMetricScale(metric);

/** Distance → whole-number similarity for display, e.g. 87. Null when no score. */
export const formatSimilarity = (distance, metric = 'cosine') => {
  const sim = distanceToSimilarity(distance, metric);
  return sim == null ? null : Math.round(sim);
};

/**
 * True when a distance meets the user's minimum similarity setting.
 * Compares the *displayed* (rounded) score so filtering never contradicts what
 * the card shows — a face labelled 60 is always included at a minimum of 60.
 */
export const meetsSimilarity = (distance, minSimilarity, metric = 'cosine') => {
  const sim = formatSimilarity(distance, metric);
  return sim != null && sim >= minSimilarity;
};
