// A named GUI identity for Safari Apple Events. No shell, arbitrary script, or other-app interface.
import AppKit
import ApplicationServices
import Darwin

func fourCC(_ value: String) -> UInt32 { value.utf8.reduce(0) { ($0 << 8) | UInt32($1) } }
func permission(_ ask: Bool) -> OSStatus {
    var descriptor = AEDesc()
    let bytes = Array("com.apple.Safari".utf8)
    let created = bytes.withUnsafeBytes { AECreateDesc(DescType(fourCC("bund")), $0.baseAddress, bytes.count, &descriptor) }
    guard created == noErr else { return OSStatus(created) }
    defer { AEDisposeDesc(&descriptor) }
    return AEDeterminePermissionToAutomateTarget(&descriptor, fourCC("****"), fourCC("****"), ask)
}

func writeJSON(_ value: [String: Any], _ path: URL) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    try data.write(to: path, options: .atomic)
    chmod(path.path, 0o600)
    let fd = open(path.path, O_RDONLY)
    if fd >= 0 { fsync(fd); close(fd) }
    let parent = open(path.deletingLastPathComponent().path, O_RDONLY)
    if parent >= 0 { fsync(parent); close(parent) }
}

struct Failure: Error { let kind: String; let message: String }

final class Engine: @unchecked Sendable {
    let root = URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "BloomWebRoot") as! String).standardizedFileURL
    let build = Bundle.main.object(forInfoDictionaryKey: "BloomBuildID") as! String
    let counts = ["open": 0, "url": 1, "navigate": 2, "eval": 2, "resize": 3, "close": 2, "check": 0, "setup": 0]

    func handle(_ path: URL) -> Bool {
        let file = path.standardizedFileURL
        let directory = file.deletingLastPathComponent()
        let spool = root.appendingPathComponent("bridge")
        let identity = directory.lastPathComponent
        // Reject other directories and symlink aliases before reading or writing anything.
        guard file.lastPathComponent == "request.json", directory.deletingLastPathComponent().path == spool.path,
              file.resolvingSymlinksInPath() == file, identity.count == 32,
              identity.allSatisfy({ $0.isHexDigit }) else { return false }
        let response = directory.appendingPathComponent("response.json")
        if FileManager.default.fileExists(atPath: response.path) { return false }
        var result: [String: Any] = ["id": identity, "build_id": build, "ok": false,
            "helper_pid": ProcessInfo.processInfo.processIdentifier, "at": Date().timeIntervalSince1970]
        var showSetup = false
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
            guard (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
                  (attributes[.size] as? NSNumber)?.intValue ?? Int.max <= 65536 else {
                throw Failure(kind: "invalid", message: "Invalid request owner or size")
            }
            let data = try Data(contentsOf: file)
            guard let request = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  request["id"] as? String == identity, request["protocol"] as? Int == 1,
                  let action = request["action"] as? String, let count = counts[action],
                  let args = request["args"] as? [String], args.count == count,
                  let deadline = request["deadline"] as? Double,
                  request["build_id"] as? String == build else {
                throw Failure(kind: "invalid", message: "Request protocol, build, identity or action mismatch")
            }
            guard args.allSatisfy({ $0.utf8.count <= 16384 && !$0.contains("\0") }) else {
                throw Failure(kind: "invalid", message: "Invalid Safari argument")
            }
            let cancelled = directory.appendingPathComponent("cancelled")
            func expired() -> Bool { Date().timeIntervalSince1970 >= deadline || FileManager.default.fileExists(atPath: cancelled.path) }
            guard !expired(), deadline <= Date().timeIntervalSince1970 + 65 else {
                throw Failure(kind: "expired", message: "Safari request expired or was cancelled before execution")
            }
            let started = directory.appendingPathComponent("started.json")
            let claim = open(started.path, O_WRONLY | O_CREAT | O_EXCL, 0o600)
            guard claim >= 0 else {
                throw Failure(kind: "uncertain", message: "This Safari request was already accepted; its effect will not be replayed")
            }
            close(claim)
            try writeJSON(["id": identity, "action": action, "helper_pid": ProcessInfo.processInfo.processIdentifier,
                           "at": Date().timeIntervalSince1970, "stage": "accepted"], started)
            if action == "setup" {
                showSetup = true; result["ok"] = true; result["stdout"] = "Safari permission setup opened"
            } else {
                let status = permission(false)
                guard status == noErr else {
                    throw Failure(kind: "permission", message: "Bloom Browser Helper needs Safari Automation access (\(status)). Open its setup window; service requests never prompt automatically.")
                }
                guard !expired() else { throw Failure(kind: "expired", message: "Safari request expired during the permission check; no operation started") }
                if action == "check" {
                    result["ok"] = true; result["stdout"] = "Safari automation allowed"
                } else {
                    if action != "open" {
                        guard let window = Int(args[0]), window > 0 else { throw Failure(kind: "invalid", message: "Invalid owned Safari window ID") }
                    }
                    if action == "eval" {
                        let js = URL(fileURLWithPath: args[1]).standardizedFileURL
                        guard js.resolvingSymlinksInPath() == js, js.path.hasPrefix(root.path + "/"), js.pathExtension == "js" else {
                            throw Failure(kind: "invalid", message: "Safari JavaScript must be an owned workspace/web file")
                        }
                    }
                    if action == "navigate" {
                        guard let url = URLComponents(string: args[1]), ["http", "https"].contains(url.scheme ?? ""),
                              url.host != nil, url.user == nil, url.password == nil else {
                            throw Failure(kind: "invalid", message: "Only normal HTTP(S) navigation is allowed")
                        }
                    }
                    if action == "resize" {
                        guard let width = Int(args[1]), [512, 1024].contains(width), let height = Int(args[2]), (512...2048).contains(height) else {
                            throw Failure(kind: "invalid", message: "Invalid Safari viewport size")
                        }
                    }
                    try writeJSON(["id": identity, "action": action, "helper_pid": ProcessInfo.processInfo.processIdentifier,
                                   "at": Date().timeIntervalSince1970, "stage": "executing"], started)
                    let process = Process()
                    process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
                    process.arguments = [Bundle.main.url(forResource: "safari", withExtension: "applescript")!.path, action] + args
                    let outputPath = directory.appendingPathComponent("stdout.txt")
                    let errorPath = directory.appendingPathComponent("stderr.txt")
                    let outputFD = open(outputPath.path, O_WRONLY | O_CREAT | O_EXCL, 0o600)
                    let errorFD = open(errorPath.path, O_WRONLY | O_CREAT | O_EXCL, 0o600)
                    guard outputFD >= 0, errorFD >= 0 else {
                        if outputFD >= 0 { close(outputFD) }; if errorFD >= 0 { close(errorFD) }
                        throw Failure(kind: "invalid", message: "Response files already exist or cannot be created")
                    }
                    let output = FileHandle(fileDescriptor: outputFD, closeOnDealloc: true)
                    let error = FileHandle(fileDescriptor: errorFD, closeOnDealloc: true)
                    process.standardOutput = output; process.standardError = error
                    try process.run()
                    var interrupted = false
                    while process.isRunning {
                        if expired() { interrupted = true; process.terminate(); break }
                        usleep(20_000)
                    }
                    if interrupted {
                        let stop = Date().addingTimeInterval(2)
                        while process.isRunning && Date() < stop { usleep(20_000) }
                        if process.isRunning { kill(process.processIdentifier, SIGKILL) }
                    }
                    process.waitUntilExit()
                    try output.close(); try error.close()
                    if interrupted { throw Failure(kind: "uncertain", message: "Safari operation cancelled or timed out; an effect may already have occurred") }
                    let errorText = String(data: try Data(contentsOf: errorPath), encoding: .utf8) ?? "Non-UTF8 AppleScript error"
                    guard process.terminationStatus == 0 else {
                        throw Failure(kind: "operation", message: String(errorText.prefix(1800)))
                    }
                    let size = try FileManager.default.attributesOfItem(atPath: outputPath.path)[.size] as! NSNumber
                    guard size.intValue <= 8_000_000 else { throw Failure(kind: "large", message: "Safari bridge response exceeds 8MB; original response retained") }
                    guard let text = String(data: try Data(contentsOf: outputPath), encoding: .utf8) else {
                        throw Failure(kind: "encoding", message: "Safari response is not UTF-8")
                    }
                    result["ok"] = true; result["stdout"] = text; result["exit_code"] = process.terminationStatus
                }
            }
        } catch let error as Failure {
            result["kind"] = error.kind; result["error"] = error.message
        } catch {
            result["kind"] = "io"; result["error"] = error.localizedDescription
        }
        try? writeJSON(result, response)
        return showSetup
    }
}

@MainActor
final class Helper: NSObject, NSApplicationDelegate {
    let engine = Engine()
    let queue = DispatchQueue(label: "bloom.safari.requests")
    var window: NSWindow?
    var status: NSTextField?
    var button: NSButton?

    func applicationDidFinishLaunching(_ notification: Notification) {
        try? writeJSON(["pid": ProcessInfo.processInfo.processIdentifier, "build_id": engine.build,
                       "app": Bundle.main.bundleURL.path, "started_at": Date().timeIntervalSince1970],
                      engine.root.appendingPathComponent("native/runtime.json"))
        if !ProcessInfo.processInfo.arguments.contains("--serve") { showSetup() }
    }

    func application(_ sender: NSApplication, openFiles filenames: [String]) {
        let engine = self.engine
        for filename in filenames {
            queue.async {
                let setup = engine.handle(URL(fileURLWithPath: filename))
                if setup { DispatchQueue.main.async { self.showSetup() } }
            }
        }
        sender.reply(toOpenOrPrint: .success)
    }

    func showSetup() {
        if window == nil {
            let view = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 570, height: 230),
                styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
            view.title = "Bloom Browser Helper — Safari Access"
            let title = NSTextField(labelWithString: "Bloom’s signed-in Safari connection")
            title.font = .boldSystemFont(ofSize: 20); title.frame = NSRect(x: 24, y: 177, width: 522, height: 28)
            let description = NSTextField(wrappingLabelWithString: "Bloom uses this named helper to open and read its own Safari research windows. Your existing Safari sessions stay in Safari. Service requests never display permission prompts.")
            description.frame = NSRect(x: 24, y: 107, width: 522, height: 60)
            status = NSTextField(wrappingLabelWithString: "Checking existing permission…")
            status!.frame = NSRect(x: 24, y: 56, width: 522, height: 44)
            button = NSButton(title: "Request Safari access", target: self, action: #selector(requestAccess))
            button!.frame = NSRect(x: 20, y: 15, width: 220, height: 32)
            for item in [title, description, status!, button!] as [NSView] { view.contentView!.addSubview(item) }
            view.center(); window = view
        }
        NSApp.setActivationPolicy(.regular); window!.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        updatePermission(false)
    }

    @objc func requestAccess() { updatePermission(true) }
    func updatePermission(_ ask: Bool) {
        button?.isEnabled = false
        status?.stringValue = ask ? "Waiting for macOS to decide Safari access…" : "Checking without prompting…"
        DispatchQueue.global(qos: .userInitiated).async {
            let result = permission(ask)
            DispatchQueue.main.async {
                self.status?.stringValue = result == noErr ? "Safari access is enabled. Bloom can now test the full browser connection." : "Safari access is not available (\(result)). Use Request Safari access to ask macOS."
                self.button?.isEnabled = result != noErr
                try? writeJSON(["status": result, "askUserIfNeeded": ask, "at": Date().timeIntervalSince1970,
                               "build_id": self.engine.build, "pid": ProcessInfo.processInfo.processIdentifier],
                              self.engine.root.appendingPathComponent("native/permission.json"))
            }
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main struct Main {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = Helper(); app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
