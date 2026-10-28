for (const w of workspace.windowList()) {
 if (w.pid === 221543 && String(w.internalId) === "{2b2d5d3c-1dcf-40f1-bf83-d6f2bc6c0688}" && String(w.resourceClass).toLowerCase().indexOf("freecad") !== -1) { w.closeWindow(); }
}
