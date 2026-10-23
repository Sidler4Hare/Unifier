
const marker = "ML-LIFE-debian13-A-001";
const pids = [];
const hint = "";
const windowId = "{12eccece-773c-46c6-a5b0-1d9b513f59bf}";
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
callDBus("org.multilinux.WindowProbe.pe8f7d3d1f6ab485e95063f0b37bf6a8a", '/org/multilinux/WindowProbe', 'org.multilinux.WindowProbe', 'Report', JSON.stringify(found));
