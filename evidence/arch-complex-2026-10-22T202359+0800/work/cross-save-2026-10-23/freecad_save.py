
import json, os
from pathlib import Path
import FreeCAD as App
import Part

project = Path(os.environ["ML_PROJECT"])
doc = App.newDocument("CrossSave")
box = doc.addObject("Part::Box", "Box")
box.Length, box.Width, box.Height = 10, 20, 30
doc.recompute()
model = project / "零件.FCStd"
step = project / "零件.step"
doc.saveAs(str(model))
Part.export([box], str(step))
first_volume = box.Shape.Volume
box.Length = 11
doc.recompute()
doc.save()
report = {
    "filename": doc.FileName,
    "length_after_second_save": box.Length.Value,
    "volume_at_step_export": first_volume,
    "volume_after_second_save": box.Shape.Volume,
    "cwd": os.getcwd(),
    "home": os.environ.get("HOME"),
    "uid": os.getuid(),
}
(project / "freecad-save.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
