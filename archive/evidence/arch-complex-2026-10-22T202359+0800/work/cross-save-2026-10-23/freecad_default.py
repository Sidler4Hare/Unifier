
import json, os
from pathlib import Path
import FreeCAD as App

doc = App.newDocument("NoPath")
save_error = None
try:
    doc.save()
except Exception as exc:
    save_error = repr(exc)
relative_error = None
try:
    doc.saveAs("相对零件.FCStd")
    relative_name = doc.FileName
except Exception as exc:
    relative_error = repr(exc)
    relative_name = doc.FileName
home_error = None
home_name = None
try:
    destination = os.path.expanduser("~/默认家目录.FCStd")
    doc2 = App.newDocument("HomeDefault")
    box = doc2.addObject("Part::Box", "Box")
    box.Length, box.Width, box.Height = 1, 1, 1
    doc2.recompute()
    doc2.saveAs(destination)
    home_name = doc2.FileName
except Exception as exc:
    home_error = repr(exc)
report = {"cwd": os.getcwd(), "home": os.environ.get("HOME"), "uid": os.getuid(),
          "save_without_name_error": save_error, "file_after_failed_save": doc.FileName,
          "relative_error": relative_error, "relative_filename": relative_name,
          "home_error": home_error, "home_filename": home_name}
Path(os.environ["ML_REPORT"]).write_text(json.dumps(report, ensure_ascii=False, indent=2))
