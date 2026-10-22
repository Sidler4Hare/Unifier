
const marker = "Wayland \u5b9e\u6d4b debian13.txt";
const pids = [];
let found = [];
for (const w of workspace.windowList()) {
    if ((marker.length > 0 && String(w.caption).indexOf(marker) !== -1) || pids.indexOf(w.pid) !== -1) {
        if (true) { w.minimized = false; workspace.activeWindow = w; }
        found.push({caption: String(w.caption), resourceClass: String(w.resourceClass),
                    pid: w.pid, wayland: w.waylandClient, x11: w.x11Client,
                    internalId: String(w.internalId), active: workspace.activeWindow === w});
    }
}
callDBus("org.multilinux.WindowProbe.p086b3bf88d9b4093a1fd621999ed89dd", '/org/multilinux/WindowProbe', 'org.multilinux.WindowProbe', 'Report', JSON.stringify(found));
