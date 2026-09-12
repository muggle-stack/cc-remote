// Local Finder/Dock entry only. The official signed App is never modified.
import AppKit
import Foundation

struct LauncherConfig: Decodable {
    let python: String
    let cwd: String
    let app: String
    let profile: String
    let state_dir: String
}

final class LauncherDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.global(qos: .userInitiated).async {
            var errorMessage: String?
            do {
                guard let url = Bundle.main.url(forResource: "launcher", withExtension: "json") else {
                    throw NSError(domain: "Launcher", code: 1)
                }
                let config = try JSONDecoder().decode(LauncherConfig.self, from: Data(contentsOf: url))
                let task = Process()
                task.executableURL = URL(fileURLWithPath: config.python)
                task.currentDirectoryURL = URL(fileURLWithPath: config.cwd)
                task.arguments = ["-m", "cc_remote.codex_desktop", "launch", "--app", config.app,
                                  "--profile", config.profile, "--state-dir", config.state_dir]
                let output = Pipe()
                task.standardInput = FileHandle.nullDevice
                task.standardOutput = output
                task.standardError = FileHandle.nullDevice
                try task.run()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                task.waitUntilExit()
                let response = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                if response?["ok"] as? Bool != true {
                    errorMessage = response?["message"] as? String ?? "共享启动未完成，请稍后重试。"
                }
            } catch {
                errorMessage = "共享入口的运行环境不可用，请重新安装共享入口。官方 App 和会话未被改动。"
            }
            let message = errorMessage
            DispatchQueue.main.async {
                if let message = message {
                    let alert = NSAlert()
                    alert.messageText = "Codex Shared"
                    alert.informativeText = message
                    alert.addButton(withTitle: "知道了")
                    NSApp.activate(ignoringOtherApps: true)
                    alert.runModal()
                }
                NSApp.terminate(nil)
            }
        }
    }

    // Finder coalesces rapid clicks while the first launch is still checking.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        return false
    }
}

let application = NSApplication.shared
let delegate = LauncherDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
