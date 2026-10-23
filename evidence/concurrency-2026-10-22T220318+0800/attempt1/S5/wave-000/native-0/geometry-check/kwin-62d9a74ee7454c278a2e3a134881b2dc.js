
const marker = "ML-CONC-native-000-0";
const pids = [];
const hint = "";
const windowId = "{099c358a-5c28-413e-b4da-fac244d59ef3}";
let found = [];
for (const w of workspace.windowList()) {
    const owned = (marker.length > 0 && String(w.caption).indexOf(marker) !== -1) ||
                  (pids.indexOf(w.pid) !== -1 && (hint.length === 0 || String(w.resourceClass).toLowerCase().indexOf(hint) !== -1));
    if (owned && (windowId.length === 0 || String(w.internalId) === windowId)) {
        if (false) { w.minimized = false; workspace.activeWindow = w; }
        found.push({caption: String(w.caption), resourceClass: String(w.resourceClass),
                    pid: w.pid, objectType: String(w),
                    wayland: typeof w.waylandClient === 'boolean' ? w.waylandClient : null,
                    x11: typeof w.x11Client === 'boolean' ? w.x11Client : null,
                    width: w.frameGeometry.width, height: w.frameGeometry.height,
                    bufferWidth: w.bufferGeometry.width, bufferHeight: w.bufferGeometry.height,
                    internalId: String(w.internalId), active: workspace.activeWindow === w});
    }
}
callDBus("org.multilinux.WindowProbe.p62d9a74ee7454c278a2e3a134881b2dc", '/org/multilinux/WindowProbe', 'org.multilinux.WindowProbe', 'Report', JSON.stringify(found));
