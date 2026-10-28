for (const w of workspace.windowList()) {
 if (w.pid === 160241 && String(w.internalId) === "{c1b9ff98-2708-4fb2-acbc-ae9f508aab4f}" && String(w.resourceClass).toLowerCase().indexOf("freecad") !== -1) { w.closeWindow(); }
}
