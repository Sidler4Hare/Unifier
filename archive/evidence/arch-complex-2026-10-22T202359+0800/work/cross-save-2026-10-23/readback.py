
import json, os
from pathlib import Path
import FreeCAD as App
import Part
project = Path(os.environ["ML_PROJECT"])
backup = App.openDocument(str(project / "零件.20261023-030530.FCBak"))
box = backup.getObject("Box")
backup.recompute()
shape = Part.Shape()
shape.read(str(project / "零件.step"))
report = {"backup_length": box.Length.Value, "backup_volume": box.Shape.Volume,
          "step_volume": shape.Volume, "step_valid": shape.isValid()}
(project / "adjacent-readback.json").write_text(json.dumps(report))
