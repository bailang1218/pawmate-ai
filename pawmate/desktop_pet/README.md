# PawMate Desktop Pet

This package owns desktop-pet assets, loop definitions, demos, and the PySide6
overlay runtime.

Rendering rule: source PNG frames are manually aligned. The viewer only scales
the full original image proportionally. Do not crop by alpha, re-center by
bounding box, or apply per-action size normalization.

Asset folders:

- `assets/standing`
- `assets/sitting_work`
- `assets/sleeping`

Standing preview:

```powershell
python -m pawmate.desktop_pet.demos.standing_demo --signals all --width-cm 4 --height-cm 6
```

Sitting/work preview:

```powershell
python -m pawmate.desktop_pet.demos.sitting_work_demo --signals all --width-cm 4 --height-cm 6
python -m pawmate.desktop_pet.app --loop sitting_work --dry-run
```

Sleeping preview:

```powershell
python -m pawmate.desktop_pet.app --loop sleeping --dry-run
python -m pawmate.desktop_pet.app --loop sleeping --mode carousel --action 16_fall_asleep --action 17_sleeping_idle --action 18_waking_up
```

Compatibility entry:

```powershell
python tools\standing_pet_carousel.py
```
