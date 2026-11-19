import Foundation

/// Best-effort preflight for the only prerequisites the app cannot bundle.
/// The CLI itself (Python 3.12 + sources) lives inside the app; stack setup
/// still needs git, a compiler and libsodium on the Mac.
struct Prerequisite: Identifiable {
    let name: String
    let detail: String
    let installCommand: String
    let satisfied: Bool

    var id: String { name }
}

enum Prerequisites {
    static func check() -> [Prerequisite] {
        let git = executableExists("git")
        let compiler = executableExists("gcc")
        let sodium = libraryExists("libsodium.dylib")
        return [
            Prerequisite(
                name: "Python 3.12 CLI",
                detail: "Bundled in this app — nothing to install.",
                installCommand: "",
                satisfied: true
            ),
            Prerequisite(
                name: "git + C compiler",
                detail: "Xcode Command Line Tools supply both; setup needs them to fetch and compile upstream sources.",
                installCommand: "xcode-select --install",
                satisfied: git && compiler
            ),
            Prerequisite(
                name: "libsodium",
                detail: "Moonraker's libnacl needs it. Homebrew is one option; setup verifies the real import.",
                installCommand: "brew install libsodium",
                satisfied: sodium
            ),
        ]
    }

    private static func searchDirs() -> [String] {
        let env = ProcessInfo.processInfo.environment["PATH"] ?? ""
        var dirs = env.split(separator: ":").map(String.init)
        dirs += ["/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin"]
        var seen = Set<String>()
        return dirs.filter { seen.insert($0).inserted }
    }

    private static func executableExists(_ name: String) -> Bool {
        let fm = FileManager.default
        return searchDirs().contains { dir in
            fm.isExecutableFile(atPath: (dir as NSString).appendingPathComponent(name))
        }
    }

    private static func libraryExists(_ name: String) -> Bool {
        let fm = FileManager.default
        var dirs = ["/usr/lib", "/usr/local/lib", "/opt/homebrew/lib"]
        dirs += searchDirs().map { ($0 as NSString).deletingLastPathComponent + "/lib" }
        return dirs.contains { dir in
            fm.fileExists(atPath: (dir as NSString).appendingPathComponent(name))
        }
    }
}
