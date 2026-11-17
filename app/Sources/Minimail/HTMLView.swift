import SwiftUI
import WebKit

/// Shows an HTML email safely:
/// - JavaScript is off, forms cannot submit, the page cannot navigate.
/// - Remote content (images, fonts, styles) is blocked unless `remoteContent` is true,
///   so tracking pixels cannot see when or where the mail is opened.
/// - Clicked links open in the default browser.
struct HTMLView: NSViewRepresentable {
    let html: String
    let remoteContent: Bool

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.defaultWebpagePreferences.allowsContentJavaScript = false
        config.websiteDataStore = .nonPersistent()  // no cookies kept between emails
        let view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = context.coordinator
        view.setValue(false, forKey: "drawsBackground")
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        let page = Self.page(html, remoteContent: remoteContent)
        guard context.coordinator.loaded != page else { return }
        context.coordinator.loaded = page
        view.loadHTMLString(page, baseURL: nil)
    }

    static func page(_ html: String, remoteContent: Bool) -> String {
        let remote = remoteContent ? " https: http:" : ""
        let csp = "default-src 'none'; img-src data:\(remote); style-src 'unsafe-inline'\(remote); "
            + "font-src data:\(remote); media-src data:\(remote); form-action 'none'; base-uri 'none'"
        return """
        <!doctype html><html><head><meta charset="utf-8">
        <meta http-equiv="Content-Security-Policy" content="\(csp)">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
          html { background: #fff; color: #000; }
          body { margin: 16px 24px; font: 14px -apple-system, BlinkMacSystemFont, sans-serif; word-wrap: break-word; }
          img { max-width: 100%; height: auto; }
          table { max-width: 100%; }
        </style></head><body>\(html)</body></html>
        """
    }

    /// True when the email loads images or styles from the internet.
    static func hasRemoteContent(_ html: String) -> Bool {
        html.range(of: #"(src|background)\s*=\s*["']?https?://|url\(\s*["']?https?://"#,
                   options: [.regularExpression, .caseInsensitive]) != nil
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        var loaded: String?

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if action.navigationType == .other, action.request.url?.absoluteString == "about:blank" {
                decisionHandler(.allow)  // our own loadHTMLString
                return
            }
            if action.navigationType == .linkActivated, let url = action.request.url,
               ["http", "https", "mailto"].contains(url.scheme?.lowercased() ?? "") {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }
    }
}
