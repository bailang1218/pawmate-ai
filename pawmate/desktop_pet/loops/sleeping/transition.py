from __future__ import annotations

from pathlib import Path

from .config import DEFAULT_NEUTRAL_FRAME, ActionPlan, CrossfadeFrame, FrameComparison


class TransitionPlanner:
    """Select a nearby source frame and add only tightly gated crossfades."""

    def __init__(self, neutral_path: Path, bridge_hold: int, crossfade_frames: int) -> None:
        self.neutral_path = neutral_path
        self.bridge_hold = bridge_hold
        self.crossfade_frames = crossfade_frames
        self._rgba_cache: dict[Path, object] = {}

    def _small_rgba(self, path: Path):
        import numpy as np
        from PIL import Image

        cached = self._rgba_cache.get(path)
        if cached is not None:
            return cached
        with Image.open(path) as image:
            small = image.convert("RGBA").resize((192, 256), Image.Resampling.BILINEAR)
        array = np.asarray(small, dtype=np.float32)
        self._rgba_cache[path] = array
        return array

    def compare(self, from_path: Path, to_path: Path) -> FrameComparison:
        import numpy as np

        if from_path == to_path:
            return FrameComparison(0.0, 1.0, 0.0, 0.0, 0.0)
        a = self._small_rgba(from_path)
        b = self._small_rgba(to_path)
        alpha_a = a[:, :, 3]
        alpha_b = b[:, :, 3]
        mask_a = alpha_a >= 64
        mask_b = alpha_b >= 64
        union = np.logical_or(mask_a, mask_b)
        if not np.any(union):
            return FrameComparison(999.0, 0.0, 999.0, 999.0, 999.0)

        intersection = np.logical_and(mask_a, mask_b)
        mask_iou = float(intersection.sum() / union.sum())

        def center(mask):
            ys, xs = np.nonzero(mask)
            if len(xs) == 0:
                return np.array([999.0, 999.0])
            return np.array([xs.mean(), ys.mean()])

        center_delta = float(np.linalg.norm(center(mask_a) - center(mask_b)))
        diff = np.abs(a - b)
        rgb_rms = float(np.sqrt(np.mean(np.square(diff[:, :, :3][union]))))
        alpha_rms = float(np.sqrt(np.mean(np.square(diff[:, :, 3][union]))))
        score = rgb_rms * 0.55 + alpha_rms * 0.9 + center_delta * 1.2 + (1.0 - mask_iou) * 145.0
        return FrameComparison(score, mask_iou, center_delta, alpha_rms, rgb_rms)

    def can_soft_crossfade(self, from_path: Path, to_path: Path) -> bool:
        comparison = self.compare(from_path, to_path)
        return (
            comparison.score <= 55.0
            and comparison.mask_iou >= 0.97
            and comparison.center_delta <= 4.5
            and comparison.alpha_rms <= 32.0
            and comparison.rgb_rms <= 42.0
        )

    def can_exit_toward(self, from_path: Path, target_path: Path) -> bool:
        if from_path.name == DEFAULT_NEUTRAL_FRAME.name:
            return True
        return self.can_soft_crossfade(from_path, target_path)

    def make_crossfade_frames(self, from_path: Path, to_path: Path) -> list[CrossfadeFrame]:
        if self.crossfade_frames <= 0 or from_path == to_path:
            return []
        if not self.can_soft_crossfade(from_path, to_path):
            return []
        return [
            CrossfadeFrame(from_path=from_path, to_path=to_path, alpha=index / (self.crossfade_frames + 1))
            for index in range(1, self.crossfade_frames + 1)
        ]

    def choose_entry_index(self, from_path: Path | None, sequence: list[tuple[Path, ActionPlan]]) -> int:
        if from_path is None or not sequence:
            return 0
        plan = sequence[0][1]
        max_candidates = min(len(sequence), 12 if plan.mode == "cyclic" else 8)
        best_index = 0
        best_score = self.compare(from_path, sequence[0][0]).score
        seen: set[Path] = set()
        for index in range(max_candidates):
            path, _ = sequence[index]
            if path in seen:
                continue
            seen.add(path)
            score = self.compare(from_path, path).score
            if score < best_score:
                best_index = index
                best_score = score
        return best_index

    def maybe_rotate_sequence(
        self,
        sequence: list[tuple[Path, ActionPlan]],
        entry_index: int,
    ) -> list[tuple[Path, ActionPlan]]:
        if entry_index <= 0 or not sequence:
            return sequence
        plan = sequence[0][1]
        if plan.mode == "return":
            return sequence[entry_index:]
        if plan.mode != "cyclic":
            return sequence
        return sequence[entry_index:] + sequence[:entry_index]

    def build_transition(
        self,
        from_path: Path | None,
        target_sequence: list[tuple[Path, ActionPlan]],
        allow_neutral_fallback: bool = True,
    ) -> list[tuple[Path | CrossfadeFrame, ActionPlan]]:
        if not target_sequence:
            return []
        entry_index = self.choose_entry_index(from_path, target_sequence)
        sequence = self.maybe_rotate_sequence(target_sequence, entry_index)
        target_path = sequence[0][0]
        plan = sequence[0][1]
        if from_path is None:
            return list(sequence)

        direct = self.make_crossfade_frames(from_path, target_path)
        if direct:
            return [(frame, plan) for frame in direct] + list(sequence)
        if not allow_neutral_fallback:
            return list(sequence)

        prefix: list[tuple[Path | CrossfadeFrame, ActionPlan]] = []
        prefix.extend((frame, plan) for frame in self.make_crossfade_frames(from_path, self.neutral_path))
        prefix.extend((self.neutral_path, plan) for _ in range(self.bridge_hold))
        prefix.extend((frame, plan) for frame in self.make_crossfade_frames(self.neutral_path, target_path))
        return prefix + list(sequence)
