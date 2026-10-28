for (const w of workspace.windowList()) {
 if (w.pid === 376300 && String(w.internalId) === "{b125823a-47e0-48b7-9a71-98e0092ffb21}" && String(w.resourceClass).toLowerCase().indexOf("freecad") !== -1) { w.closeWindow(); }
}
