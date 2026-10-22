
const marker = "Wayland \u5b9e\u6d4b debian13.txt";
const pids = [];
let found = [];
for (const w of workspace.windowList()) {
    if ((marker.length > 0 && String(w.caption).indexOf(marker) !== -1) || pids.indexOf(w.pid) !== -1) {
        if (false) { w.minimized = false; workspace.activeWindow = w; }
        found.push({caption: String(w.caption), resourceClass: String(w.resourceClass),
                    pid: w.pid, objectType: String(w),
                    wayland: typeof w.waylandClient === 'boolean' ? w.waylandClient : null,
                    x11: typeof w.x11Client === 'boolean' ? w.x11Client : null,
                    internalId: String(w.internalId), active: workspace.activeWindow === w});
    }
}
callDBus("org.multilinux.WindowProbe.pab505db29c24492f8dced480bba99512", '/org/multilinux/WindowProbe', 'org.multilinux.WindowProbe', 'Report', JSON.stringify(found));
