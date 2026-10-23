
import json, os
from pathlib import Path
import FreeCAD as App

project = Path(os.environ["ML_PROJECT"])
doc = App.openDocument(str(project / "零件.FCStd"))
box = doc.getObject("Box")
doc.recompute()
report = {"filename": doc.FileName, "length": box.Length.Value,
          "volume": box.Shape.Volume, "uid": os.getuid()}
(project / "freecad-reopen.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
