import Foundation

// ---------------------------------------------------------------------------
// `klipperformac serial --json` — the multi-MCU wire contract.
//
// The CLI owns this shape, the GUI decodes it. Pinned literally here (and in
// tests/contract/, where `serial_json.golden` freezes the producer's exact
// stdout and `test_json_bytes_contract.py` asserts this file's `contractFixture`
// against it) so a renamed key is a deliberate change to two files instead of a
// silently empty pane:
//
// {
//   "mcus": [
//     {
//       "mcu": "mcu",                       // "mcu" = timing master, else "tool"
//       "section": "[mcu tool]",            // as spelled in printer.cfg
//       "serial": "/dev/cu.usbmodem14101",  // or null; a CAN board's uuid here
//       "configured": true,                 // section carries serial:/canbus
//       "claimed": true,                    // a USB node is held by this section
//       "identity": {                        // or null (no IOKit answer)
//         "vid_pid": "1d50:614e",
//         "serial": "5044506110A45B1C",
//         "key": "usb:1d50:614e:5044506110A45B1C",
//         "present": true
//       },
//       "conflict": null                    // or, in the precedence the CLI
//                                           // ranks by: "bad-section-name" |
//                                           // "duplicate-port" | "missing-port"
//     }
//   ],
//   "single": true                          // one MCU: nothing to fan out
// }
//
// `claimed` and `serial` are different questions: a CAN section has a serial
// field (its uuid) and claims no node, so "not claimed" never means "not
// configured" — and a row that lost its badge would be a CAN board the GUI
// offered a USB cable for.
//
// The CLI promises exit 0 and "conflict strings, never a traceback" in
// read-only mode. A promise is not input validation: everything below degrades
// on its own, because the alternative to a decoded default is a crash inside
// the app that is watching someone's print.
// ---------------------------------------------------------------------------

/// The three conflicts `serial --json` can name, listed in the precedence the
/// CLI ranks them by (cause before symptom): a section klipper would read as a
/// different object is the bug, and the dead port it names is what that bug
/// causes. Unknown values are kept as text (see McuRow.conflictRaw) so a fourth
/// kind added on the CLI side shows up as a badge instead of vanishing from the
/// GUI. `tests/contract/test_json_bytes_contract.py` pins this set against
/// `mcus.CONFLICT_CODES` and `cli._MCU_CONFLICTS`, so a code added on one side
/// and not the others is a red test, not a row with no badge.
enum McuConflict: String {
    case badSectionName = "bad-section-name"
    case duplicatePort = "duplicate-port"
    case missingPort = "missing-port"

    /// Badge text — has to fit one row.
    var label: String {
        switch self {
        case .duplicatePort: return "port shared"
        case .missingPort: return "port missing"
        case .badSectionName: return "bad section"
        }
    }

    /// Hover text: what Klipper will do if this is left alone, and what fixes it.
    var tooltip: String {
        switch self {
        case .duplicatePort:
            return "Two [mcu] sections in printer.cfg point at this same port. "
                + "Klipper opens a serial device once, so the stack refuses to "
                + "start. Give every board its own /dev/cu path."
        case .missingPort:
            return "printer.cfg names a port that is not there right now, so "
                + "klippy would sit in startup. Re-plug the board, or pick the "
                + "path it has now."
        case .badSectionName:
            return "This section name is not one Klipper accepts — only [mcu] "
                + "and [mcu <name>] are. Edit printer.cfg to fix the spelling."
        }
    }
}

/// identity: {vid_pid, serial, key, present} — macOS has no /dev/serial/by-id,
/// so this is the IOKit answer that keeps a port renumbering from becoming a
/// mystery. `key` is decoded but not drawn: it is the same board the tail
/// already names, and a row is not the place to dump a device path.
struct McuIdentity: Equatable {
    let vidPid: String?
    let serial: String?
    let key: String?
    let present: Bool
}

struct McuRow: Identifiable, Equatable {
    /// Name used to address the section on the command line
    /// (`serial --set PATH <mcu>`), so it doubles as the row identity.
    let mcu: String
    let section: String
    let serial: String?
    let configured: Bool
    /// Decoded to pin the contract shape and to keep change detection honest;
    /// deliberately not rendered — "port claimed" next to a "port shared"
    /// badge would read as two findings when the CLI means one.
    let claimed: Bool
    let identity: McuIdentity?
    let conflict: McuConflict?
    /// Conflict value the CLI sent when it is not one of the known three.
    let conflictRaw: String?

    var id: String { mcu }

    var isMaster: Bool { mcu == McuRow.masterName }
    static let masterName = "mcu"

    /// Section names come from a hand-edited file, and a control character in
    /// one would break the row's layout. Display stays honest — the character
    /// shows up as "?" — while the write path refuses it (isAddressable),
    /// because a row you cannot see is a config problem you cannot fix.
    var displayName: String {
        var out = String.UnicodeScalarView()
        for scalar in mcu.unicodeScalars {
            out.append(scalar.value < 0x20 || scalar.value == 0x7F ? "?" : scalar)
        }
        return String(out)
    }

    /// Whether this name can be handed to `serial --set PATH <name>`.
    ///
    /// Three refusals, each one mirroring what the CLI does with the name — a
    /// button that the command line is going to reject is not a choice, it is a
    /// dead end with an error toast:
    ///  * an empty name or one carrying a control character (`isSafeArgument`);
    ///  * a name that is not its own trimmed form: `serial --set` trims the
    ///    argument before it builds a header, so ' board' would be looked up as
    ///    'board', match nothing, and die — while the section klipper really
    ///    registered is one no one can name from a pin (`klippy/pins.py:71`
    ///    stops pin translation at the first space);
    ///  * a section the producer already flagged `bad-section-name`: its header
    ///    is not the pin identity klipper registered (`klippy/mcu.py:1151-1158`
    ///    strips exactly the four characters `mcu `), so writing a port into it
    ///    points a cable at a board klipper cannot address. The badge and its
    ///    tooltip tell the user what to fix instead of offering a picker that
    ///    cannot work.
    var isAddressable: Bool {
        SerialReportDecoder.isSafeArgument(mcu)
            && mcu == mcu.trimmingCharacters(in: .whitespacesAndNewlines)
            && conflict != .badSectionName
    }

    /// A port picker only makes sense for rows whose config language is a
    /// device path. A CAN row already carries its own uuid in `serial`, so
    /// offering `/dev/cu.*` there would be fiction instead of a fix.
    var offersPortPicker: Bool {
        guard isAddressable else { return false }
        guard let serial else { return true }
        return serial.hasPrefix("/dev/")
    }

    /// The same three verdicts `serial` prints in its own table, so the GUI and
    /// the terminal can never tell a config two different stories.
    var configuredLine: String {
        if let serial { return "Configured: \(serial)" }
        return configured ? "No serial set" : "Not in printer.cfg"
    }

    /// Identity tail with the present y/n the row needs; a board the scanner
    /// never saw says so instead of pretending to be offline.
    var identityLine: String {
        guard let identity else { return "identity: not detected" }
        var bits: [String] = []
        if let vidPid = identity.vidPid { bits.append("USB \(vidPid)") }
        if let serial = identity.serial { bits.append("SER \(serial)") }
        if bits.isEmpty, let key = identity.key { bits.append(key) }
        if bits.isEmpty { bits.append("identity") }
        return bits.joined(separator: "  ") + "  ·  present: \(identity.present ? "y" : "n")"
    }

    var isPresent: Bool? { identity?.present }

    /// Tooltip for the badge, or nil when there is nothing to warn about.
    var conflictTooltip: String? {
        if let conflict { return conflict.tooltip }
        let raw = conflictRaw?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return raw.isEmpty ? nil : "The command line flags this MCU: \(raw)."
    }

    var conflictLabel: String { conflict?.label ?? "check config" }
}

struct SerialReport: Equatable {
    let mcus: [McuRow]
    let single: Bool

    var isEmpty: Bool { mcus.isEmpty }
}

/// Defensive decode. Returns nil for anything that is not this contract —
/// including an old CLI answering `error: unrecognized arguments: --json` —
/// which is exactly the signal the caller needs to fall back to the single
/// board picker unchanged.
enum SerialReportDecoder {
    /// The contract, byte for byte: this literal IS
    /// `tests/contract/serial_json.golden`, the frozen stdout of
    /// `klipperformac serial --json` over `tests/contract/canon_multi_mcu.cfg`
    /// with the faked live facts in `canon_scan.json` (no port opened, no ioreg).
    /// `tests/contract/test_json_bytes_contract.py` asserts these bytes against
    /// the producer, so changing the JSON on one side is a red test on the other
    /// instead of a silently empty MCU pane. Re-record with
    /// KLIPPERFORMAC_FREEZE_GOLDEN=1 and re-paste here in the same commit.
    static let contractFixture = """
    {"mcus": [{"mcu": "mcu", "section": "[mcu]", "serial": "/dev/cu.usbmodem14101"\
    , "configured": true, "claimed": true, "identity": {"vid_pid": "1d50:614e"\
    , "serial": "5044506110A45B1C", "key": "usb:1d50:614e:5044506110A45B1C"\
    , "present": true}, "conflict": null}, {"mcu": " board"\
    , "section": "[mcu  board]", "serial": "/dev/cu.usbmodem14198"\
    , "configured": true, "claimed": true, "identity": null\
    , "conflict": "bad-section-name"}, {"mcu": "analyser"\
    , "section": "[mcu analyser]", "serial": "/dev/cu.usbserial-1420"\
    , "configured": true, "claimed": true, "identity": {"vid_pid": "1a86:7523"\
    , "serial": "00A1B2C3", "key": "usb:1a86:7523:00A1B2C3", "present": true}\
    , "conflict": "duplicate-port"}, {"mcu": "camera", "section": "[mcu camera]"\
    , "serial": "/dev/cu.usbmodem14199", "configured": true, "claimed": true\
    , "identity": null, "conflict": "missing-port"}, {"mcu": "rpi"\
    , "section": "[mcu rpi]", "serial": "112233445566", "configured": true\
    , "claimed": false, "identity": null, "conflict": null}, {"mcu": "scanner"\
    , "section": "[mcu scanner]", "serial": "/dev/cu.usbserial-1420"\
    , "configured": true, "claimed": true, "identity": {"vid_pid": "1a86:7523"\
    , "serial": "00A1B2C3", "key": "usb:1a86:7523:00A1B2C3", "present": true}\
    , "conflict": "duplicate-port"}, {"mcu": "tool", "section": "[mcu tool]"\
    , "serial": null, "configured": true, "claimed": false\
    , "identity": {"vid_pid": "1a86:7523", "serial": "FFFF0001"\
    , "key": "usb:1a86:7523:FFFF0001", "present": false}\
    , "conflict": "missing-port"}], "single": false}
    """

    static func decode(_ output: String) -> SerialReport? {
        guard let data = jsonObjectBytes(in: output),
              let root = try? JSONSerialization.jsonObject(with: data, options: [.allowFragments]),
              let dict = root as? [String: Any],
              // "mcus" must be an array of objects: its absence, or a null /
              // string / number where the array should be, means "not this
              // contract" rather than "no MCUs".
              let entries = dict["mcus"] as? [[String: Any]]
        else { return nil }

        var rows: [McuRow] = []
        var names = Set<String>()
        for entry in entries {
            guard let row = row(from: entry), names.insert(row.mcu).inserted else { continue }
            rows.append(row)
        }
        // A duplicated mcu name would give SwiftUI two rows with one id, so
        // only the first survives; the CLI dedups the same way upstream.
        let single = bool(dict["single"], default: rows.count <= 1)
        return SerialReport(mcus: rows, single: single)
    }

    /// Old CLIs reject `serial --json` at argument parsing time. A transient
    /// timeout or mixed stderr noise is different: the app should keep the last
    /// honest answer instead of downgrading the whole pane to "legacy".
    static func looksLikeLegacyCLI(_ output: String, exitCode: Int32? = nil) -> Bool {
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }
        let lower = trimmed.lowercased()
        if lower.contains("unrecognized arguments: --json")
            || lower.contains("no such option: --json")
            || lower.contains("unknown option: --json")
        { return true }
        if let exitCode, exitCode == 2, lower.contains("usage:"),
           lower.contains("serial"), lower.contains("--json")
        { return true }
        return false
    }

    // MARK: - envelope

    /// stdout and stderr share one pipe in CLIRunner, so the JSON can arrive
    /// with a preamble line or a trailing hint. Take the outermost brace pair
    /// first, then the whole text, and give up rather than guess: a decode we
    /// are not sure about becomes the legacy picker, never a half-shown board.
    private static func jsonObjectBytes(in output: String) -> Data? {
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let open = trimmed.firstIndex(of: "{"),
              let close = trimmed.lastIndex(of: "}"), close > open else { return nil }
        for candidate in [String(trimmed[open...close]), trimmed] {
            guard let data = candidate.data(using: .utf8),
                  let root = try? JSONSerialization.jsonObject(with: data),
                  root is [String: Any]
            else { continue }
            return data
        }
        return nil
    }

    private static func row(from entry: [String: Any]) -> McuRow? {
        let section = rawText(entry["section"]) ?? ""
        let rawName = rawText(entry["mcu"]) ?? ""
        // Old CLIs and hand-edited output can name the MCU either way; a
        // section is the fallback because "[mcu tool]" still tells us both
        // that it is an MCU and what it is called.
        let name = rawName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? mcuName(fromSection: section)
            : rawName
        guard let name, !name.isEmpty else {
            // Nothing to label the row with, so nothing to show: every real
            // section name, however malformed, keeps its row (and its badge).
            return nil
        }
        let serial = text(entry["serial"])
        let configured = bool(entry["configured"], default: serial != nil)
        let identity = McuIdentity(from: entry["identity"])
        let conflictText = text(entry["conflict"])
        return McuRow(mcu: name,
                      section: section.isEmpty ? "[mcu \(name)]" : section,
                      serial: serial,
                      configured: configured,
                      claimed: bool(entry["claimed"], default: false),
                      identity: identity,
                      conflict: conflictText.flatMap(McuConflict.init(rawValue:)),
                      conflictRaw: conflictText)
    }

    /// Klipper spells these sections `[mcu]` and `[mcu <name>]`; anything else
    /// is not an MCU row and must not be offered a port.
    static func mcuName(fromSection section: String) -> String? {
        var s = section.trimmingCharacters(in: .whitespacesAndNewlines)
        guard s.hasPrefix("["), s.hasSuffix("]") else { return nil }
        s.removeFirst()
        s.removeLast()
        guard s.lowercased().hasPrefix(McuRow.masterName) else { return nil }
        guard s.count >= McuRow.masterName.count else { return nil }
        if s == McuRow.masterName { return McuRow.masterName }
        let separator = s[s.index(s.startIndex, offsetBy: McuRow.masterName.count)]
        guard separator == " " || separator == "\t" else { return nil }
        return String(s[s.index(after: s.index(s.startIndex,
                                               offsetBy: McuRow.masterName.count))...])
    }

    /// Arguments reach the CLI as an array, never through a shell, so the only
    /// real hazard is a name that cannot be one (control characters, newlines:
    /// the same set `serial --set` refuses in its own device-path check).
    static func isSafeArgument(_ name: String) -> Bool {
        guard !name.isEmpty, name.count <= 64 else { return false }
        return name.unicodeScalars.allSatisfy { !($0.value < 0x20 || $0.value == 0x7F) }
    }

    // MARK: - value readers (typed strictly, no coercion surprises)

    private static func text(_ value: Any?) -> String? {
        guard let s = value as? String else { return nil }
        let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func rawText(_ value: Any?) -> String? {
        value as? String
    }

    private static func bool(_ value: Any?, default fallback: Bool) -> Bool {
        if let b = value as? Bool { return b }
        return fallback
    }
}

extension McuIdentity {
    /// nil when the key is absent, null, or not an object — "no identity", not
    /// "a board with unknown fields".
    init?(from value: Any?) {
        guard let dict = value as? [String: Any] else { return nil }
        func text(_ v: Any?) -> String? {
            guard let s = v as? String else { return nil }
            let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
            return t.isEmpty ? nil : t
        }
        let present = (dict["present"] as? Bool) ?? false
        let id = McuIdentity(vidPid: text(dict["vid_pid"]),
                             serial: text(dict["serial"]),
                             key: text(dict["key"]),
                             present: present)
        // {} from a CLI that found nothing looks like a board with no name.
        if id.vidPid == nil, id.serial == nil, id.key == nil, !id.present { return nil }
        self = id
    }
}
