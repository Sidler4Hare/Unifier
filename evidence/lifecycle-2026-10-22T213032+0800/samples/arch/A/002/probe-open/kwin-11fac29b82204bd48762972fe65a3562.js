
const marker = "ML-LIFE-arch-A-002";
const pids = [];
const hint = "";
const windowId = "{7bf663ac-1e10-4cff-821c-2828a79ff191}";
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
callDBus("org.multilinux.WindowProbe.p11fac29b82204bd48762972fe65a3562", '/org/multilinux/WindowProbe', 'org.multilinux.WindowProbe', 'Report', JSON.stringify(found));
