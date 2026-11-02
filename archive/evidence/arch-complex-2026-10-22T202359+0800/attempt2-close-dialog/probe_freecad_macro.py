"""FreeCAD GUI macro payload. Run as a .FCMacro inside an isolated lab app.

The driver supplies ML_CAD_WORK and ML_CAD_PHASE. Geometry checks are against
analytical volume; STEP is re-imported rather than merely checked for existence.
"""
import os, json, math, traceback
from pathlib import Path
import FreeCAD as App
import FreeCADGui as Gui
import Part
from PySide import QtCore, QtWidgets

def probe():
    work = Path(os.environ['ML_CAD_WORK'])
    phase = os.environ['ML_CAD_PHASE']
    model = work / '中文 模型.FCStd'
    step = work / '中文 模型.step'
    result = {'phase': phase, 'passed': False, 'version': App.Version(), 'uid': os.getuid()}
    try:
        if phase == 'build':
            doc = App.newDocument('MultilinuxCAD')
            box = doc.addObject('Part::Box', 'Box')
            box.Length, box.Width, box.Height = 40, 30, 20
            hole = doc.addObject('Part::Cylinder', 'Hole')
            hole.Radius, hole.Height = 5, 20
            hole.Placement.Base = App.Vector(20, 15, 0)
            cut = doc.addObject('Part::Cut', 'Cut')
            cut.Base, cut.Tool = box, hole
            doc.recompute()
            before = cut.Shape.Volume
            hole.Radius = 6
            doc.recompute()
            expected = 40*30*20 - math.pi*6**2*20
            assert abs(cut.Shape.Volume - expected) < 1e-6
            assert before > cut.Shape.Volume
            assert cut.Shape.isValid() and len(cut.Shape.Solids) == 1
            box.Visibility = False
            hole.Visibility = False
            cut.Visibility = True
            doc.saveAs(str(model))
            Part.export([cut], str(step))
            result['parameter_recompute'] = {'before_volume': before, 'after_volume': cut.Shape.Volume}
        else:
            doc = App.openDocument(str(model))
            cut = doc.getObject('Cut')
            expected = 40*30*20 - math.pi*6**2*20
            assert doc.getObject('Hole').Radius.Value == 6
            assert cut.Shape.isValid() and len(cut.Shape.Solids) == 1
            assert abs(cut.Shape.Volume - expected) < 1e-6
        imported = Part.Shape()
        imported.read(str(step))
        assert imported.isValid() and len(imported.Solids) == 1
        assert abs(imported.Volume-expected) < 1e-5
        Gui.activateWorkbench('PartWorkbench')
        Gui.activeDocument().activeView().viewAxonometric()
        Gui.activeDocument().activeView().fitAll()
        Gui.getMainWindow().setWindowTitle('Multilinux FreeCAD '+phase)
        Gui.getMainWindow().resize(1100, 760)
        Gui.getMainWindow().show()
        for tabs in Gui.getMainWindow().findChildren(QtWidgets.QTabBar):
            for index in range(tabs.count()):
                if '中文 模型' in tabs.tabText(index):
                    tabs.setCurrentIndex(index)
        Gui.updateGui()
        Gui.activeDocument().activeView().saveImage(str(work/(phase+'-viewport.png')), 1000, 700, 'White')
        result.update(passed=True, volume=cut.Shape.Volume, expected_volume=expected,
                      step_volume=imported.Volume, solids=len(cut.Shape.Solids), valid=cut.Shape.isValid(),
                      qt_platform=QtWidgets.QApplication.instance().platformName())
    except Exception:
        result['error'] = traceback.format_exc()
    (work/(phase+'-result.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    # Exit through the application's normal event loop after allowing window evidence.
    QtCore.QTimer.singleShot(25000, Gui.getMainWindow().close)

QtCore.QTimer.singleShot(3000, probe)
