
const marker = "multilinux-nonexistent-window-marker";
const pids = [];
let found = [];
for (const w of workspace.windowList()) {
    if ((marker.length > 0 && String(w.caption).indexOf(marker) !== -1) || pids.indexOf(w.pid) !== -1) {
        if (false) { w.minimized = false; workspace.activeWindow = w; }
        found.push({caption: String(w.caption), resourceClass: String(w.resourceClass),
                    pid: w.pid, wayland: w.waylandClient, x11: w.x11Client,
                    internalId: String(w.internalId), active: workspace.activeWindow === w});
    }
}
callDBus("org.multilinux.WindowProbe.pd1914ff7abd444c5b2cab2483e425782", '/org/multilinux/WindowProbe', 'org.multilinux.WindowProbe', 'Report', JSON.stringify(found));
