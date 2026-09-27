// PeachyHead: AirPods head orientation -> JSON lines on stdout.
//
//   {"t":<s>,"age":<ms>,"q":[w,x,y,z],"rr":[x,y,z]}     one per motion sample
//   {"ev":"connected"|"disconnected"|"denied"|"unavailable"|"started"}
//
// `t` is the sample's CoreMotion timestamp (seconds of uptime), `age` how old it
// was when written. The process exits when stdin closes (the parent went away).

import CoreMotion
import Darwin
import Foundation

setvbuf(stdout, nil, _IOLBF, 0)
signal(SIGPIPE, SIG_DFL)

// TCC charges the Motion permission to the "responsible" process: the terminal
// or editor that started us, which has no NSMotionUsageDescription and gets us
// killed. Re-spawn ourselves disclaimed so the permission is PeachyHead's own;
// stdio is inherited, so the pipe to the parent keeps working.
let disclaimedKey = "PEACHY_HEAD_DISCLAIMED"
if ProcessInfo.processInfo.environment[disclaimedKey] == nil {
    typealias Disclaim = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
    if let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "responsibility_spawnattrs_setdisclaim") {
        let disclaim = unsafeBitCast(sym, to: Disclaim.self)
        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        _ = disclaim(&attr, 1)
        setenv(disclaimedKey, "1", 1)
        let path = Bundle.main.executablePath ?? CommandLine.arguments[0]
        var argv: [UnsafeMutablePointer<CChar>?] = CommandLine.arguments.map { strdup($0) } + [nil]
        var pid: pid_t = 0
        if posix_spawn(&pid, path, nil, &attr, &argv, environ) == 0 {
            for sig in [SIGINT, SIGTERM, SIGHUP] {
                signal(sig, SIG_IGN)
                let src = DispatchSource.makeSignalSource(signal: sig, queue: .global())
                src.setEventHandler { kill(pid, sig) }
                src.resume()
                _ = Unmanaged.passRetained(src)
            }
            var status: Int32 = 0
            while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
            let code = status & 0x7f
            exit(code == 0 ? (status >> 8) & 0xff : 128 + code)
        }
    }
}

func emit(_ line: String) {
    FileHandle.standardOutput.write((line + "\n").data(using: .utf8)!)
}

func f(_ v: Double) -> String { String(format: "%.6f", v) }

final class Watcher: NSObject, CMHeadphoneMotionManagerDelegate {
    func headphoneMotionManagerDidConnect(_ manager: CMHeadphoneMotionManager) {
        emit("{\"ev\":\"connected\"}")
    }

    func headphoneMotionManagerDidDisconnect(_ manager: CMHeadphoneMotionManager) {
        emit("{\"ev\":\"disconnected\"}")
    }
}

let manager = CMHeadphoneMotionManager()
let watcher = Watcher()
manager.delegate = watcher

switch CMHeadphoneMotionManager.authorizationStatus() {
case .denied, .restricted:
    emit("{\"ev\":\"denied\"}")
    exit(2)
default:
    break
}

guard manager.isDeviceMotionAvailable else {
    emit("{\"ev\":\"unavailable\"}")
    exit(3)
}

let queue = OperationQueue()
queue.maxConcurrentOperationCount = 1
queue.qualityOfService = .userInteractive

manager.startConnectionStatusUpdates()
manager.startDeviceMotionUpdates(to: queue) { motion, error in
    if let error = error as NSError? {
        if error.domain == CMErrorDomain && error.code == Int(CMErrorMotionActivityNotAuthorized.rawValue) {
            emit("{\"ev\":\"denied\"}")
            exit(2)
        }
        emit("{\"ev\":\"error\",\"msg\":\"\(error.localizedDescription.replacingOccurrences(of: "\"", with: "'"))\"}")
        return
    }
    guard let m = motion else { return }
    let q = m.attitude.quaternion
    let r = m.rotationRate
    let age = (ProcessInfo.processInfo.systemUptime - m.timestamp) * 1000.0
    emit("{\"t\":\(f(m.timestamp)),\"age\":\(String(format: "%.1f", age)),"
        + "\"q\":[\(f(q.w)),\(f(q.x)),\(f(q.y)),\(f(q.z))],"
        + "\"rr\":[\(f(r.x)),\(f(r.y)),\(f(r.z))]}")
}
emit("{\"ev\":\"started\"}")

Thread.detachNewThread {
    while let chunk = try? FileHandle.standardInput.read(upToCount: 64), !chunk.isEmpty {}
    manager.stopDeviceMotionUpdates()
    exit(0)
}

dispatchMain()
