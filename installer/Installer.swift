import Cocoa
import Darwin

/// A deliberately small native shell around the bundled installation backend.
/// The backend owns installation; this process owns only UI, logs, and its child
/// process group. It never opens Terminal or changes IINA's running state.
private struct InstallerEvent {
    let type: String
    let stage: String?
    let label: String?
    let detail: String?
    let progress: Double?
    let message: String?
    let iinaPath: String?
    let logPath: String?

    init?(line: String) {
        guard let data = line.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = object["type"] as? String else { return nil }
        self.type = type
        self.stage = object["stage"] as? String
        self.label = object["label"] as? String
        self.detail = object["detail"] as? String
        self.message = object["message"] as? String
        self.iinaPath = object["iina_path"] as? String
        self.logPath = object["log_path"] as? String
        if let number = object["progress"] as? NSNumber {
            let value = number.doubleValue
            self.progress = value.isFinite ? min(1, max(0, value)) : nil
        } else {
            self.progress = nil
        }
    }
}

private final class InstallerProcess {
    private var pid: pid_t = 0
    private var output: Pipe?
    private var errors: Pipe?
    private let queue = DispatchQueue(label: "io.github.avslyv.live-translation.installer-output")
    private var outputBuffer = Data()
    private var errorBuffer = Data()
    private var didFinish = false
    var onLine: ((String, Bool) -> Void)?
    var onExit: ((Int32) -> Void)?

    /// POSIX_SPAWN_SETPGROUP creates a fresh process group with the child's PID.
    /// Cancellation can then signal exactly our backend and its descendants.
    func start(script: URL, arguments: [String] = []) throws {
        let stdout = Pipe()
        let stderr = Pipe()
        var actions: posix_spawn_file_actions_t?
        var attributes: posix_spawnattr_t?
        posix_spawn_file_actions_init(&actions)
        posix_spawnattr_init(&attributes)
        defer {
            posix_spawn_file_actions_destroy(&actions)
            posix_spawnattr_destroy(&attributes)
        }
        posix_spawn_file_actions_adddup2(&actions, stdout.fileHandleForWriting.fileDescriptor, STDOUT_FILENO)
        posix_spawn_file_actions_adddup2(&actions, stderr.fileHandleForWriting.fileDescriptor, STDERR_FILENO)
        posix_spawn_file_actions_addclose(&actions, stdout.fileHandleForReading.fileDescriptor)
        posix_spawn_file_actions_addclose(&actions, stderr.fileHandleForReading.fileDescriptor)
        posix_spawn_file_actions_addclose(&actions, stdout.fileHandleForWriting.fileDescriptor)
        posix_spawn_file_actions_addclose(&actions, stderr.fileHandleForWriting.fileDescriptor)
        posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF))
        posix_spawnattr_setpgroup(&attributes, 0)
        var mask = sigset_t()
        sigemptyset(&mask)
        posix_spawnattr_setsigmask(&attributes, &mask)
        var defaults = sigset_t()
        sigemptyset(&defaults)
        for signal in [SIGTERM, SIGINT, SIGHUP, SIGPIPE] { sigaddset(&defaults, signal) }
        posix_spawnattr_setsigdefault(&attributes, &defaults)
        let args = ["/bin/bash", script.path] + arguments
        var environment = ProcessInfo.processInfo.environment
        environment["LT_INSTALLER_GUI"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        let allocatedArgs = args.map { strdup($0) }
        let allocatedEnvironment = environment.map { strdup("\($0.key)=\($0.value)") }
        defer {
            allocatedArgs.forEach { free($0) }
            allocatedEnvironment.forEach { free($0) }
        }
        var argv = allocatedArgs + [nil]
        var envp = allocatedEnvironment + [nil]
        var child: pid_t = 0
        let result = posix_spawn(&child, "/bin/bash", &actions, &attributes, &argv, &envp)
        guard result == 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(result), userInfo: [
                NSLocalizedDescriptionKey: "Could not start setup: \(String(cString: strerror(result)))"
            ])
        }
        pid = child
        output = stdout
        errors = stderr
        try? stdout.fileHandleForWriting.close()
        try? stderr.fileHandleForWriting.close()
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.queue.async { [weak self] in
                guard let self, !self.didFinish else { return }
                self.drain(handle, isError: false)
            }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.queue.async { [weak self] in
                guard let self, !self.didFinish else { return }
                self.drain(handle, isError: true)
            }
        }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            var status: Int32 = 0
            while waitpid(child, &status, 0) == -1 && errno == EINTR {}
            let exitCode = (status & 0x7f) == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f)
            self?.queue.async { [weak self] in
                guard let self else { return }
                self.output?.fileHandleForReading.readabilityHandler = nil
                self.errors?.fileHandleForReading.readabilityHandler = nil
                self.drain(stdout.fileHandleForReading, isError: false)
                self.drain(stderr.fileHandleForReading, isError: true)
                self.flush(isError: false)
                self.flush(isError: true)
                self.didFinish = true
                DispatchQueue.main.async { self.onExit?(exitCode) }
            }
        }
    }

    var diagnosticIdentity: String { "pid=\(pid), group=\(getpgid(pid))" }

    @discardableResult func cancel() -> Bool {
        guard pid > 0 else { return false }
        let child = pid
        // A negative PID addresses only the group created by posix_spawn above.
        let sent = getpgid(child) == child && kill(-child, SIGTERM) == 0
        // Allow the backend time to stop a child and restore an existing venv.
        queue.asyncAfter(deadline: .now() + 15) { [weak self] in
            guard let self, !self.didFinish, getpgid(child) == child else { return }
            kill(-child, SIGKILL)
        }
        return sent
    }

    private func receive(_ data: Data, isError: Bool) {
        if data.isEmpty {
            if isError { errors?.fileHandleForReading.readabilityHandler = nil }
            else { output?.fileHandleForReading.readabilityHandler = nil }
            flush(isError: isError)
            return
        }
        if isError { errorBuffer.append(data) } else { outputBuffer.append(data) }
        var buffer = isError ? errorBuffer : outputBuffer
        while let newline = buffer.firstIndex(of: 10) {
            let line = String(decoding: buffer[..<newline], as: UTF8.self)
            buffer.removeSubrange(...newline)
            DispatchQueue.main.async { [weak self] in self?.onLine?(line, isError) }
        }
        // A subprocess progress display can use carriage returns rather than
        // newlines. Bound the retained tail without losing a complete JSON line.
        if buffer.count > 256 * 1024 {
            let text = String(decoding: buffer, as: UTF8.self)
            buffer.removeAll(keepingCapacity: true)
            DispatchQueue.main.async { [weak self] in self?.onLine?(text, isError) }
        }
        if isError { errorBuffer = buffer } else { outputBuffer = buffer }
    }

    private func flush(isError: Bool) {
        let buffer = isError ? errorBuffer : outputBuffer
        guard !buffer.isEmpty else { return }
        let line = String(decoding: buffer, as: UTF8.self)
        if isError { errorBuffer.removeAll() } else { outputBuffer.removeAll() }
        DispatchQueue.main.async { [weak self] in self?.onLine?(line, isError) }
    }

    private func drain(_ handle: FileHandle, isError: Bool) {
        let descriptor = handle.fileDescriptor
        let flags = fcntl(descriptor, F_GETFL)
        _ = fcntl(descriptor, F_SETFL, flags | O_NONBLOCK)
        var bytes = [UInt8](repeating: 0, count: 8192)
        while true {
            let count = Darwin.read(descriptor, &bytes, bytes.count)
            if count == 0 { receive(Data(), isError: isError); break }
            guard count > 0 else { break }
            receive(Data(bytes.prefix(count)), isError: isError)
        }
    }
}

private final class StageRow: NSView {
    private let icon = NSImageView()
    private let title = NSTextField(labelWithString: "")
    private let detail = NSTextField(wrappingLabelWithString: "")

    init(title: String, detail: String) {
        super.init(frame: .zero)
        self.title.stringValue = title
        self.title.font = .systemFont(ofSize: 13, weight: .semibold)
        self.detail.stringValue = detail
        self.detail.font = .systemFont(ofSize: 11.5)
        self.detail.textColor = .secondaryLabelColor
        icon.translatesAutoresizingMaskIntoConstraints = false
        icon.symbolConfiguration = .init(pointSize: 19, weight: .regular)
        let labels = NSStackView(views: [self.title, self.detail])
        labels.orientation = .vertical
        labels.alignment = .leading
        labels.spacing = 3
        labels.translatesAutoresizingMaskIntoConstraints = false
        addSubview(icon)
        addSubview(labels)
        NSLayoutConstraint.activate([
            icon.leadingAnchor.constraint(equalTo: leadingAnchor),
            icon.topAnchor.constraint(equalTo: topAnchor, constant: 1),
            icon.widthAnchor.constraint(equalToConstant: 24),
            icon.heightAnchor.constraint(equalToConstant: 24),
            labels.leadingAnchor.constraint(equalTo: icon.trailingAnchor, constant: 12),
            labels.trailingAnchor.constraint(equalTo: trailingAnchor),
            labels.topAnchor.constraint(equalTo: topAnchor),
            labels.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
        setState(.pending)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    enum State { case pending, active, complete }
    func setState(_ state: State) {
        switch state {
        case .pending:
            icon.image = NSImage(systemSymbolName: "circle", accessibilityDescription: "Pending")
            icon.contentTintColor = .tertiaryLabelColor
            title.textColor = .secondaryLabelColor
        case .active:
            icon.image = NSImage(systemSymbolName: "arrow.down.circle.fill", accessibilityDescription: "In progress")
            icon.contentTintColor = .controlAccentColor
            title.textColor = .labelColor
        case .complete:
            icon.image = NSImage(systemSymbolName: "checkmark.circle.fill", accessibilityDescription: "Complete")
            icon.contentTintColor = .systemGreen
            title.textColor = .labelColor
        }
    }
}

private final class InstallerDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private var primary: NSButton!
    private var secondary: NSButton!
    private var detailsButton: NSButton!
    private var logButton: NSButton!
    private var headline: NSTextField!
    private var statusDetail: NSTextField!
    private var progress: NSProgressIndicator!
    private var progressLabel: NSTextField!
    private var detailsContainer: NSView!
    private var detailsHeight: NSLayoutConstraint!
    private var logText: NSTextView!
    private var stages: [StageRow] = []
    private var child: InstallerProcess?
    private var isRunning = false
    private var succeeded = false
    private var wasCancelled = false
    private var completeMessage: String?
    private var backendError: String?
    private var iinaPath = "/Applications/IINA.app"
    private var demoTimer: Timer?
    private var demoIndex = 0
    private var activeStage = 0
    private var quitAfterStop = false
    private let demo = CommandLine.arguments.contains("--demo")
    private let demoFailure = CommandLine.arguments.contains("--demo-failure")
    private let backendDryRun = CommandLine.arguments.contains("--backend-dry-run")
    private var isPreview: Bool { demo || demoFailure }
    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/LiveTranslationInstaller/frontend.log")
    private var backendLogURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/LiveTranslationInstaller/installer.log")
    private var logHandle: FileHandle?

    func applicationDidFinishLaunching(_ notification: Notification) {
        configureMenu()
        configureWindow()
        prepareLog()
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if !isRunning { return .terminateNow }
        if wasCancelled {
            // Do not send a second signal while the backend restores state.
            quitAfterStop = true
            return .terminateLater
        }
        let alert = NSAlert()
        alert.messageText = "Stop installation?"
        alert.informativeText = "Setup is still running. Downloaded files are kept where possible so you can try again later."
        alert.addButton(withTitle: "Keep Installing")
        alert.addButton(withTitle: "Stop and Quit")
        if alert.runModal() == .alertSecondButtonReturn {
            quitAfterStop = true
            wasCancelled = true
            child?.cancel()
            demoTimer?.invalidate()
            if isPreview { finish(exitCode: 130) }
            return .terminateLater
        }
        return .terminateCancel
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if !isRunning { return true }
        secondaryAction(nil)
        return false
    }

    private func configureMenu() {
        let menu = NSMenu()
        let application = NSMenuItem()
        let applicationMenu = NSMenu()
        applicationMenu.addItem(withTitle: "About Live Translation Setup", action: #selector(showAbout), keyEquivalent: "")
        applicationMenu.addItem(.separator())
        applicationMenu.addItem(withTitle: "Quit Live Translation Setup", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        application.submenu = applicationMenu
        menu.addItem(application)
        let edit = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        edit.submenu = editMenu
        menu.addItem(edit)
        NSApp.mainMenu = menu
    }

    private func configureWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 650, height: 680),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Live Translation Setup"
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        let root = NSView()
        root.wantsLayer = true
        window.contentView = root

        let iconBackground = NSView()
        iconBackground.wantsLayer = true
        iconBackground.layer?.backgroundColor = NSColor.controlAccentColor.withAlphaComponent(0.12).cgColor
        iconBackground.layer?.cornerRadius = 16
        let icon = NSImageView(image: NSImage(systemSymbolName: "captions.bubble.fill", accessibilityDescription: "Subtitles") ?? NSImage())
        icon.contentTintColor = .controlAccentColor
        icon.symbolConfiguration = .init(pointSize: 30, weight: .medium)
        icon.translatesAutoresizingMaskIntoConstraints = false
        iconBackground.addSubview(icon)
        NSLayoutConstraint.activate([
            icon.centerXAnchor.constraint(equalTo: iconBackground.centerXAnchor),
            icon.centerYAnchor.constraint(equalTo: iconBackground.centerYAnchor),
        ])
        let title = NSTextField(labelWithString: "Live Translation for IINA")
        title.font = .systemFont(ofSize: 25, weight: .bold)
        let subtitle = NSTextField(labelWithString: "Local subtitles. Native playback.")
        subtitle.font = .systemFont(ofSize: 14)
        subtitle.textColor = .secondaryLabelColor
        let titles = NSStackView(views: [title, subtitle])
        titles.orientation = .vertical
        titles.alignment = .leading
        titles.spacing = 6
        let header = NSStackView(views: [iconBackground, titles])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 18
        iconBackground.widthAnchor.constraint(equalToConstant: 64).isActive = true
        iconBackground.heightAnchor.constraint(equalToConstant: 64).isActive = true

        let introduction = NSTextField(wrappingLabelWithString:
            "Install the plugin, background service, and local models for this user. No Terminal or Homebrew required.")
        introduction.font = .systemFont(ofSize: 13)
        introduction.textColor = .secondaryLabelColor
        introduction.maximumNumberOfLines = 3

        stages = [
            StageRow(title: "Check your Mac", detail: "Apple silicon · macOS 14 or later"),
            StageRow(title: "Install the essentials", detail: "Python, FFmpeg, Ollama, and IINA"),
            StageRow(title: "Download local models", detail: "Whisper Turbo + Gemma 4 12B"),
            StageRow(title: "Connect native subtitles", detail: "Plugin, background service, and IINA launcher"),
        ]
        let stageStack = NSStackView(views: stages)
        stageStack.orientation = .vertical
        stageStack.alignment = .leading
        stageStack.spacing = 17
        stages.forEach { $0.widthAnchor.constraint(equalTo: stageStack.widthAnchor).isActive = true }

        let divider = NSBox()
        divider.boxType = .separator
        headline = NSTextField(labelWithString: isPreview ? "Setup preview" : backendDryRun ? "Check this Mac" : "Ready to install")
        headline.font = .systemFont(ofSize: 14, weight: .semibold)
        statusDetail = NSTextField(wrappingLabelWithString: isPreview
            ? "Preview mode — no software or models will be installed."
            : backendDryRun ? "Read-only check — no software or models will be installed."
            : "Initial downloads are about 10 GB. Existing models and saved subtitle caches are reused.")
        statusDetail.font = .systemFont(ofSize: 12)
        statusDetail.textColor = .secondaryLabelColor
        statusDetail.maximumNumberOfLines = 3
        progress = NSProgressIndicator()
        progress.style = .bar
        progress.isIndeterminate = false
        progress.minValue = 0
        progress.maxValue = 1
        progress.doubleValue = 0
        progressLabel = NSTextField(labelWithString: "Downloads several GB. Keep this window open during setup.")
        progressLabel.font = .systemFont(ofSize: 11)
        progressLabel.textColor = .tertiaryLabelColor
        progressLabel.lineBreakMode = .byTruncatingTail

        let content = NSStackView(views: [header, introduction, stageStack, divider, headline, statusDetail, progress, progressLabel])
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 12
        content.setCustomSpacing(18, after: header)
        content.setCustomSpacing(22, after: introduction)
        content.setCustomSpacing(22, after: stageStack)
        content.setCustomSpacing(16, after: divider)
        content.setCustomSpacing(6, after: headline)
        content.setCustomSpacing(10, after: statusDetail)
        content.setCustomSpacing(5, after: progress)
        content.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(content)
        [introduction, stageStack, divider, headline, statusDetail, progress, progressLabel].forEach {
            $0.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        }

        detailsContainer = NSView()
        detailsContainer.translatesAutoresizingMaskIntoConstraints = false
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.translatesAutoresizingMaskIntoConstraints = false
        logText = NSTextView(frame: NSRect(x: 0, y: 0, width: 570, height: 150))
        logText.isEditable = false
        logText.isSelectable = true
        logText.font = .monospacedSystemFont(ofSize: 10.5, weight: .regular)
        logText.textContainerInset = NSSize(width: 8, height: 8)
        logText.autoresizingMask = [.width]
        logText.isVerticallyResizable = true
        logText.isHorizontallyResizable = false
        logText.minSize = NSSize(width: 0, height: 50)
        logText.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        logText.textContainer?.widthTracksTextView = true
        logText.textContainer?.containerSize = NSSize(width: 570, height: CGFloat.greatestFiniteMagnitude)
        scroll.documentView = logText
        detailsContainer.addSubview(scroll)
        root.addSubview(detailsContainer)
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: detailsContainer.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: detailsContainer.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: detailsContainer.topAnchor),
            scroll.bottomAnchor.constraint(equalTo: detailsContainer.bottomAnchor),
        ])
        detailsContainer.isHidden = true
        detailsHeight = detailsContainer.heightAnchor.constraint(equalToConstant: 0)
        detailsHeight.isActive = true

        detailsButton = NSButton(title: "Show Details", target: self, action: #selector(toggleDetails(_:)))
        detailsButton.bezelStyle = .inline
        detailsButton.font = .systemFont(ofSize: 11)
        logButton = NSButton(title: "Open Log", target: self, action: #selector(openLog(_:)))
        logButton.bezelStyle = .inline
        logButton.font = .systemFont(ofSize: 11)
        logButton.isHidden = true
        let footer = NSStackView(views: [detailsButton, logButton])
        footer.orientation = .horizontal
        footer.spacing = 12
        footer.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(footer)

        secondary = NSButton(title: "Cancel", target: self, action: #selector(secondaryAction(_:)))
        secondary.bezelStyle = .rounded
        secondary.isEnabled = false
        primary = NSButton(title: isPreview ? "Start Preview" : backendDryRun ? "Check This Mac" : "Install Everything", target: self, action: #selector(primaryAction(_:)))
        primary.bezelStyle = .rounded
        primary.keyEquivalent = "\r"
        let buttons = NSStackView(views: [secondary, primary])
        buttons.orientation = .horizontal
        buttons.spacing = 10
        buttons.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(buttons)
        primary.widthAnchor.constraint(greaterThanOrEqualToConstant: 130).isActive = true

        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Development"
        let footerLabel = NSTextField(labelWithString: "\(version) · Apple silicon · Everything runs locally after setup")
        footerLabel.font = .systemFont(ofSize: 10)
        footerLabel.textColor = .tertiaryLabelColor
        footerLabel.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(footerLabel)
        NSLayoutConstraint.activate([
            content.topAnchor.constraint(equalTo: root.topAnchor, constant: 24),
            content.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 32),
            content.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -32),
            detailsContainer.topAnchor.constraint(equalTo: content.bottomAnchor, constant: 14),
            detailsContainer.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            detailsContainer.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            footer.topAnchor.constraint(equalTo: detailsContainer.bottomAnchor, constant: 10),
            footer.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            buttons.centerYAnchor.constraint(equalTo: footer.centerYAnchor),
            buttons.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            footerLabel.topAnchor.constraint(equalTo: buttons.bottomAnchor, constant: 14),
            footerLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            footerLabel.bottomAnchor.constraint(lessThanOrEqualTo: root.bottomAnchor, constant: -20),
        ])
    }

    private func prepareLog() {
        do {
            try FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
            }
            logHandle = try FileHandle(forWritingTo: logURL)
            try logHandle?.seekToEnd()
            appendLog("\n=== Live Translation setup · \(ISO8601DateFormatter().string(from: Date())) ===")
        } catch {
            appendLog("Could not create the setup log: \(error.localizedDescription)")
        }
    }

    private func appendLog(_ text: String) {
        guard !text.isEmpty else { return }
        let line = text + (text.hasSuffix("\n") ? "" : "\n")
        try? logHandle?.write(contentsOf: Data(line.utf8))
        logText?.textStorage?.append(NSAttributedString(string: line, attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 10.5, weight: .regular),
            .foregroundColor: NSColor.labelColor,
        ]))
        // Retain useful recent progress without growing the UI indefinitely.
        if let storage = logText?.textStorage, storage.length > 500_000 {
            storage.deleteCharacters(in: NSRange(location: 0, length: storage.length - 350_000))
        }
        logText?.scrollToEndOfDocument(nil)
    }

    @objc private func primaryAction(_ sender: Any?) {
        if succeeded && !isPreview && !backendDryRun {
            openIINA()
            return
        }
        guard !isRunning else { return }
        succeeded = false
        wasCancelled = false
        completeMessage = nil
        backendError = nil
        isRunning = true
        activeStage = 0
        updateStages()
        primary.isEnabled = false
        secondary.title = "Cancel"
        secondary.isEnabled = true
        headline.stringValue = "Checking your Mac…"
        statusDetail.stringValue = "Preparing local setup. Existing models and subtitle caches will be reused."
        progress.doubleValue = 0
        progressLabel.stringValue = "You can leave this window open while downloads continue."
        if isPreview { startDemo(); return }
        guard let script = Bundle.main.resourceURL?.appendingPathComponent("bootstrap.sh"),
              FileManager.default.fileExists(atPath: script.path) else {
            finish(exitCode: 1, error: "The installer is incomplete. Download the complete installer app from the GitHub release.")
            return
        }
        let process = InstallerProcess()
        child = process
        process.onLine = { [weak self] line, isError in self?.receive(line: line, isError: isError) }
        process.onExit = { [weak self] code in self?.finish(exitCode: code) }
        do { try process.start(script: script, arguments: backendDryRun ? ["--dry-run"] : []) }
        catch { finish(exitCode: 1, error: error.localizedDescription) }
    }

    private func receive(line: String, isError: Bool) {
        guard !isError, let event = InstallerEvent(line: line) else { appendLog(line); return }
        switch event.type {
        case "progress":
            if let stage = event.stage { activeStage = stageIndex(stage, progress: event.progress) }
            if let label = event.label { headline.stringValue = label }
            if let detail = event.detail { statusDetail.stringValue = detail }
            if let value = event.progress {
                progress.doubleValue = value
                progressLabel.stringValue = "\(Int(value * 100))% · Downloads and setup run locally on this Mac"
            }
            updateStages()
            if let label = event.label { appendLog(label + (event.detail.map { " — " + $0 } ?? "")) }
        case "log": appendLog(event.message ?? event.detail ?? line)
        case "complete":
            completeMessage = event.message ?? event.detail
            if let path = event.iinaPath { iinaPath = path }
            if let path = event.logPath { backendLogURL = URL(fileURLWithPath: path) }
            appendLog(event.message ?? "Installation completed.")
        case "error":
            backendError = event.message ?? event.detail ?? event.label ?? "Installation failed."
            appendLog(backendError!)
        default: appendLog(event.message ?? line)
        }
    }

    private func stageIndex(_ stage: String, progress: Double?) -> Int {
        let name = stage.lowercased()
        if name.contains("check") || name.contains("preflight") { return 0 }
        if name.contains("model") || name.contains("whisper") { return 2 }
        if name.contains("plugin") || name.contains("service") || name.contains("connect") || name.contains("integrat") || name.contains("finish") { return 3 }
        if name.contains("python") || name.contains("runtime") || name.contains("depend") || name.contains("ffmpeg") || name.contains("ollama") || name.contains("iina") || name.contains("tool") { return 1 }
        if let progress { return min(3, Int(progress * 4)) }
        return activeStage
    }

    private func updateStages() {
        for (index, row) in stages.enumerated() {
            row.setState(index < activeStage ? .complete : index == activeStage ? .active : .pending)
        }
    }

    private func finish(exitCode: Int32, error: String? = nil) {
        guard isRunning else { return }
        isRunning = false
        demoTimer?.invalidate()
        demoTimer = nil
        child = nil
        primary.isEnabled = true
        secondary.isEnabled = true
        secondary.title = "Done"
        if quitAfterStop {
            NSApp.reply(toApplicationShouldTerminate: true)
            return
        }
        if wasCancelled && exitCode != 0 {
            headline.stringValue = "Installation stopped"
            statusDetail.stringValue = "Downloaded files are kept where possible. You can resume setup whenever you are ready."
            progressLabel.stringValue = "Your saved subtitles have not been removed."
            primary.title = isPreview ? "Start Preview" : "Resume Installation"
            appendLog("Setup cancelled by the viewer.")
        } else if exitCode == 0 && backendDryRun {
            succeeded = true
            progress.doubleValue = 1
            stages.enumerated().forEach { $0.element.setState($0.offset == 0 ? .complete : .pending) }
            headline.stringValue = "Read-only check complete"
            statusDetail.stringValue = completeMessage ?? "The installer checked this Mac without installing software or downloading models."
            progressLabel.stringValue = "No changes were made. Launch setup normally to install."
            primary.title = "Run Check Again"
        } else if exitCode == 0 {
            succeeded = true
            progress.doubleValue = 1
            stages.forEach { $0.setState(.complete) }
            headline.stringValue = isPreview ? "Preview complete" : "Ready for native subtitles"
            statusDetail.stringValue = isPreview ? "No software or models were installed." :
                "Quit and reopen IINA to activate the plugin, then open a local English video. Russian subtitles will connect automatically."
            progressLabel.stringValue = isPreview ? "Run the actual installer to set up your Mac." : "Whisper and Gemma are installed locally. No cloud inference."
            primary.title = isPreview ? "Run Preview Again" : "Open IINA"
        } else {
            headline.stringValue = "Setup needs attention"
            statusDetail.stringValue = error ?? backendError ?? "Installation could not finish. Review the details below, then try again."
            progressLabel.stringValue = "Your settings backup and existing subtitle cache are kept."
            primary.title = isPreview ? "Try Preview Again" : "Try Again"
            appendLog("Setup exited with code \(exitCode). " + (error ?? backendError ?? "See the preceding log for details."))
            setDetailsVisible(true)
        }
    }

    @objc private func secondaryAction(_ sender: Any?) {
        if !isRunning { NSApp.terminate(nil); return }
        guard !wasCancelled else { return }
        wasCancelled = true
        secondary.isEnabled = false
        headline.stringValue = "Stopping setup…"
        statusDetail.stringValue = "Waiting for the current task to stop safely."
        child?.cancel()
        if isPreview { finish(exitCode: 130) }
    }

    @objc private func toggleDetails(_ sender: Any?) { setDetailsVisible(detailsContainer.isHidden) }

    private func setDetailsVisible(_ visible: Bool) {
        guard detailsContainer.isHidden == visible else { return }
        detailsContainer.isHidden = !visible
        let extraHeight = min(150, max(50, (window.screen?.visibleFrame.height ?? 900) - window.frame.height - 20))
        let previousHeight = detailsHeight.constant
        detailsHeight.constant = visible ? extraHeight : 0
        detailsButton.title = visible ? "Hide Details" : "Show Details"
        logButton.isHidden = !visible
        var frame = window.frame
        let difference: CGFloat = visible ? extraHeight : -previousHeight
        frame.origin.y -= difference
        frame.size.height += difference
        if let visibleFrame = window.screen?.visibleFrame {
            frame.origin.y = max(visibleFrame.minY, min(frame.origin.y, visibleFrame.maxY - frame.height))
        }
        window.setFrame(frame, display: true, animate: true)
    }

    @objc private func openLog(_ sender: Any?) {
        let selected = FileManager.default.fileExists(atPath: backendLogURL.path) ? backendLogURL : logURL
        NSWorkspace.shared.activateFileViewerSelecting([selected])
    }

    private func openIINA() {
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: "com.colliderli.iina")
        guard !running.isEmpty else {
            NSWorkspace.shared.open(URL(fileURLWithPath: iinaPath))
            return
        }
        let alert = NSAlert()
        alert.messageText = "Restart IINA to activate the plugin?"
        alert.informativeText = "IINA is already running. Restarting closes its current playback window. This uses a normal quit; IINA will keep its usual playback history."
        alert.addButton(withTitle: "Quit and Reopen")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        running.forEach { _ = $0.terminate() }
        primary.isEnabled = false
        primary.title = "Reopening IINA…"
        let started = Date()
        Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] timer in
            guard let self else { timer.invalidate(); return }
            let stillRunning = NSRunningApplication.runningApplications(withBundleIdentifier: "com.colliderli.iina")
            if stillRunning.isEmpty {
                timer.invalidate()
                NSWorkspace.shared.open(URL(fileURLWithPath: self.iinaPath))
                self.primary.isEnabled = true
                self.primary.title = "Open IINA"
            } else if Date().timeIntervalSince(started) > 12 {
                timer.invalidate()
                self.primary.isEnabled = true
                self.primary.title = "Open IINA"
                self.statusDetail.stringValue = "IINA is still open. Finish any IINA dialog, then quit and reopen it to activate the plugin."
            }
        }
    }

    @objc private func showAbout() {
        let alert = NSAlert()
        alert.messageText = "Live Translation for IINA"
        alert.informativeText = "Local English-to-Russian subtitles using MLX Whisper and Ollama.\n\nBuilt on KazKozDev/live-translation. MIT licensed."
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "View on GitHub")
        if alert.runModal() == .alertSecondButtonReturn,
           let url = URL(string: "https://github.com/avslyv/live-translation-iina") {
            NSWorkspace.shared.open(url)
        }
    }

    private func startDemo() {
        let samples: [(String, String, String, Double)] = [
            ("preflight", "Checking your Mac…", "Apple silicon and macOS 14 or later detected.", 0.05),
            ("runtime", "Installing the essentials…", "Preparing Python, FFmpeg, Ollama, and IINA.", 0.20),
            ("dependencies", "Preparing the local runtime…", "Creating an isolated Python environment.", 0.35),
            ("models", "Downloading local models…", "Whisper Turbo and Gemma 4 12B. Existing weights are reused.", 0.55),
            ("models", "Preparing Gemma 4…", "Downloads can take time; setup can be retried if interrupted.", 0.75),
            ("integration", "Connecting IINA…", "Installing the plugin and background service.", 0.92),
        ]
        demoIndex = 0
        demoTimer = Timer.scheduledTimer(withTimeInterval: 0.8, repeats: true) { [weak self] timer in
            guard let self else { timer.invalidate(); return }
            if self.demoIndex < samples.count {
                let item = samples[self.demoIndex]
                self.activeStage = self.stageIndex(item.0, progress: item.3)
                self.headline.stringValue = item.1
                self.statusDetail.stringValue = item.2
                self.progress.doubleValue = item.3
                self.progressLabel.stringValue = "Preview · \(Int(item.3 * 100))% · No changes are being made"
                self.updateStages()
                self.appendLog("PREVIEW: " + item.1)
                self.demoIndex += 1
                if self.demoFailure && self.demoIndex == 4 {
                    self.finish(exitCode: 1, error: "Preview: a network download was interrupted. You can retry without opening Terminal.")
                }
            } else { self.finish(exitCode: 0) }
        }
    }
}

if CommandLine.arguments.contains("--self-test") {
    let valid = InstallerEvent(line: #"{"type":"progress","stage":"models","label":"Downloading","progress":0.5}"#)
    let clamped = InstallerEvent(line: #"{"type":"progress","progress":8}"#)
    let complete = InstallerEvent(line: #"{"type":"complete","message":"Ready","iina_path":"/Applications/IINA.app"}"#)
    guard valid?.stage == "models", valid?.progress == 0.5,
          clamped?.progress == 1, complete?.iinaPath == "/Applications/IINA.app",
          InstallerEvent(line: "raw progress line") == nil,
          InstallerEvent(line: #"{"label":"Missing type"}"#) == nil else {
        fputs("Installer frontend protocol self-test failed\n", stderr)
        exit(1)
    }
    // Exercise the actual child-process transport, including split UTF-8/JSON,
    // stderr, a final line without a newline, and scoped group cancellation.
    let fixtures = FileManager.default.temporaryDirectory.appendingPathComponent("live-translation-frontend-" + UUID().uuidString)
    do {
        try FileManager.default.createDirectory(at: fixtures, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: fixtures) }
        let script = fixtures.appendingPathComponent("protocol fixture.sh")
        try #"""
        printf '{"type":"progress","stage":"models","progress":0.'
        /bin/sleep 0.05
        printf '25}\n'
        printf 'Русский текст\n'
        printf 'fixture warning\n' >&2
        printf 'tail: %s' "$1"
        exit 7
        """#.write(to: script, atomically: true, encoding: .utf8)
        let process = InstallerProcess()
        var lines = [String]()
        var errorLines = [String]()
        var result: Int32?
        process.onLine = { line, isError in
            if isError { errorLines.append(line) } else { lines.append(line) }
        }
        process.onExit = { result = $0 }
        try process.start(script: script, arguments: ["a value with spaces"])
        let timeout = Date().addingTimeInterval(5)
        while result == nil && Date() < timeout { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        guard result == 7,
              lines.contains(where: { InstallerEvent(line: $0)?.progress == 0.25 }),
              lines.contains("Русский текст"), lines.contains("tail: a value with spaces"),
              errorLines.contains("fixture warning") else {
            process.cancel()
            throw NSError(domain: "InstallerSelfTest", code: 1, userInfo: [NSLocalizedDescriptionKey: "Subprocess output transport failed"])
        }
        let cancellation = fixtures.appendingPathComponent("cancellation.sh")
        try #"""
        printf 'started\n'
        /bin/sleep 20 &
        wait
        """#.write(to: cancellation, atomically: true, encoding: .utf8)
        let cancelled = InstallerProcess()
        var cancelledResult: Int32?
        var cancellationLines = [String]()
        var signalSent = false
        cancelled.onLine = { line, _ in
            cancellationLines.append(line)
            if line == "started" { signalSent = cancelled.cancel() }
        }
        cancelled.onExit = { cancelledResult = $0 }
        try cancelled.start(script: cancellation)
        let cancellationTimeout = Date().addingTimeInterval(5)
        while cancelledResult == nil && Date() < cancellationTimeout { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        guard cancelledResult == 143 else {
            cancelled.cancel()
            throw NSError(domain: "InstallerSelfTest", code: 2, userInfo: [NSLocalizedDescriptionKey: "Scoped process cancellation failed (status: \(String(describing: cancelledResult)), lines: \(cancellationLines), signal: \(signalSent), \(cancelled.diagnosticIdentity))"])
        }
    } catch {
        fputs("Installer frontend self-test failed: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
    print("Installer frontend protocol, subprocess, and cancellation self-tests passed")
    exit(0)
}

let application = NSApplication.shared
private let delegate = InstallerDelegate()
application.setActivationPolicy(.regular)
application.delegate = delegate
application.run()
