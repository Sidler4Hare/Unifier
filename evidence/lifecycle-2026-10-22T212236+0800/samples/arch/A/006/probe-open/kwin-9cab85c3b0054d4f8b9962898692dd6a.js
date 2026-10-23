
const marker = "ML-LIFE-arch-A-006";
const pids = [];
const hint = "";
const windowId = "{2f7ef46c-8ac7-4ea2-8537-a3f821080e8a}";
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
callDBus("org.multilinux.WindowProbe.p9cab85c3b0054d4f8b9962898692dd6a", '/org/multilinux/WindowProbe', 'org.multilinux.WindowProbe', 'Report', JSON.stringify(found));
