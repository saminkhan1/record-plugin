import Foundation

enum RecorderURLSanitizer {
    private static let secretTerms: Set<String> = [
        "token",
        "auth",
        "authorization",
        "oauth",
        "oauth2",
        "code",
        "key",
        "secret",
        "session",
        "sid",
        "jwt",
        "password",
        "passwd",
        "pwd",
    ]
    private static let secretCompactKeys: Set<String> = [
        "accesstoken",
        "apikey",
        "apitoken",
        "authcode",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "idtoken",
        "oauthcode",
        "passcode",
        "refreshtoken",
        "sessionid",
        "sessionkey",
        "sig",
        "signature",
        "xamzsecuritytoken",
        "xamzsignature",
        "xgoogsignature",
    ]
    private static let secretCompactSuffixes = [
        "password",
        "passwd",
        "secret",
        "token",
    ]
    private static let urlKeys: Set<String> = [
        "url",
        "origin",
        "href",
        "before_url",
        "after_url",
        "frame_url",
        "top_url",
        "final_url",
        "referrer",
    ]
    private static let redactedValue = "%5BREDACTED%5D"

    /// Remove URL credentials while preserving replay-relevant URL bytes when
    /// no credential-bearing value is present.
    static func sanitize(_ rawValue: String) -> String {
        let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, isAbsoluteURL(value) else { return value }

        let withoutUserInfo = removingUserInfo(from: value)
        let fragmentStart = withoutUserInfo.firstIndex(of: "#")
        let beforeFragment = fragmentStart.map { String(withoutUserInfo[..<$0]) } ?? withoutUserInfo
        let fragment = fragmentStart.map { String(withoutUserInfo[withoutUserInfo.index(after: $0)...]) }

        let queryStart = beforeFragment.firstIndex(of: "?")
        let base = queryStart.map { String(beforeFragment[..<$0]) } ?? beforeFragment
        let query = queryStart.map { String(beforeFragment[beforeFragment.index(after: $0)...]) }

        var result = base
        if let query {
            result += "?" + sanitizeQuery(query)
        }
        if let fragment {
            result += "#" + sanitizeFragment(fragment)
        }
        return result
    }

    /// Recursively sanitize fields which carry URLs in native recorder payloads.
    static func sanitizePayload(_ payload: JSONDict) -> JSONDict {
        sanitizeValue(payload, key: nil) as? JSONDict ?? payload
    }

    private static func sanitizeValue(_ value: Any, key: String?) -> Any {
        if let dictionary = value as? JSONDict {
            var sanitized: JSONDict = [:]
            for (nestedKey, nestedValue) in dictionary {
                sanitized[nestedKey] = sanitizeValue(nestedValue, key: nestedKey)
            }
            return sanitized
        }
        if let array = value as? [Any] {
            return array.map { sanitizeValue($0, key: key) }
        }
        if let key, isURLKey(key), let string = value as? String {
            return sanitize(string)
        }
        return value
    }

    private static func isURLKey(_ key: String) -> Bool {
        let normalized = key.lowercased()
        return urlKeys.contains(normalized) || normalized.hasSuffix("url")
    }

    private static func isAbsoluteURL(_ value: String) -> Bool {
        if value.hasPrefix("//") { return true }
        guard let separator = value.firstIndex(of: ":") else { return false }
        let scheme = value[..<separator]
        guard let first = scheme.unicodeScalars.first, isASCIIAlpha(first) else { return false }
        return scheme.unicodeScalars.dropFirst().allSatisfy {
            isASCIIAlphaNumeric($0) || $0 == "+" || $0 == "-" || $0 == "."
        }
    }

    private static func removingUserInfo(from value: String) -> String {
        let authorityStart: String.Index
        if value.hasPrefix("//") {
            authorityStart = value.index(value.startIndex, offsetBy: 2)
        } else {
            guard let schemeEnd = value.firstIndex(of: ":") else { return value }
            let afterScheme = value.index(after: schemeEnd)
            guard value[afterScheme...].hasPrefix("//") else { return value }
            authorityStart = value.index(afterScheme, offsetBy: 2)
        }

        let authorityEnd =
            value[authorityStart...].firstIndex { character in
                character == "/" || character == "?" || character == "#"
            } ?? value.endIndex
        guard let userInfoEnd = value[authorityStart..<authorityEnd].lastIndex(of: "@") else {
            return value
        }
        return String(value[..<authorityStart])
            + String(value[value.index(after: userInfoEnd)...])
    }

    private static func sanitizeFragment(_ fragment: String) -> String {
        if let queryStart = fragment.firstIndex(of: "?") {
            let route = String(fragment[..<queryStart])
            let query = String(fragment[fragment.index(after: queryStart)...])
            let sanitized = sanitizeQuery(query)
            return sanitized == query ? fragment : route + "?" + sanitized
        }
        guard fragment.contains("=") else { return fragment }
        return sanitizeQuery(fragment)
    }

    private static func sanitizeQuery(_ query: String) -> String {
        var result = ""
        var fieldStart = query.startIndex
        var cursor = query.startIndex
        while cursor < query.endIndex {
            let character = query[cursor]
            if character == "&" || character == ";" {
                result += sanitizeQueryField(String(query[fieldStart..<cursor]))
                result.append(character)
                fieldStart = query.index(after: cursor)
            }
            cursor = query.index(after: cursor)
        }
        result += sanitizeQueryField(String(query[fieldStart...]))
        return result
    }

    private static func sanitizeQueryField(_ field: String) -> String {
        guard queryFieldHasSecretKey(field), let keyEnd = field.firstIndex(of: "=") else {
            return field
        }
        return String(field[..<keyEnd]) + "=" + redactedValue
    }

    private static func queryFieldHasSecretKey(_ field: String) -> Bool {
        guard let keyEnd = field.firstIndex(of: "=") else { return false }
        return isSecretQueryKey(canonicalQueryKey(String(field[..<keyEnd])))
    }

    private static func canonicalQueryKey(_ encodedKey: String) -> String {
        var decoded = encodedKey
        for _ in 0..<3 {
            let nextValue = decodeQueryKeyOnce(decoded)
            if nextValue == decoded { break }
            decoded = nextValue
        }
        return decoded
    }

    private static func decodeQueryKeyOnce(_ value: String) -> String {
        let scalars = Array(value.unicodeScalars)
        var result = ""
        var index = 0
        while index < scalars.count {
            let scalar = scalars[index]
            if scalar == "+" {
                result.append(" ")
                index += 1
                continue
            }
            if scalar == "%",
               index + 2 < scalars.count,
               let high = hexadecimalValue(scalars[index + 1]),
               let low = hexadecimalValue(scalars[index + 2])
            {
                let byte = high * 16 + low
                if byte <= 0x7F, let decoded = UnicodeScalar(byte) {
                    result.unicodeScalars.append(decoded)
                    index += 3
                    continue
                }
            }
            result.unicodeScalars.append(scalar)
            index += 1
        }
        return result
    }

    private static func hexadecimalValue(_ scalar: UnicodeScalar) -> UInt32? {
        switch scalar.value {
        case 48...57: return scalar.value - 48
        case 65...70: return scalar.value - 55
        case 97...102: return scalar.value - 87
        default: return nil
        }
    }

    private static func isSecretQueryKey(_ rawKey: String) -> Bool {
        let scalars = Array(rawKey.trimmingCharacters(in: .whitespacesAndNewlines).unicodeScalars)
        guard !scalars.isEmpty else { return false }
        let compact = String(String.UnicodeScalarView(scalars.filter(isASCIIAlphaNumeric))).lowercased()
        if secretCompactKeys.contains(compact)
            || secretCompactSuffixes.contains(where: { compact.hasSuffix($0) })
        {
            return true
        }

        var terms: Set<String> = []
        var current = ""
        for index in scalars.indices {
            let scalar = scalars[index]
            guard isASCIIAlphaNumeric(scalar) else {
                if !current.isEmpty {
                    terms.insert(current)
                    current = ""
                }
                continue
            }
            if index > scalars.startIndex {
                let previous = scalars[index - 1]
                let next = index + 1 < scalars.endIndex ? scalars[index + 1] : nil
                let camelBoundary =
                    (isASCIILowerOrDigit(previous) && isASCIIUpper(scalar))
                    || (isASCIIUpper(previous) && isASCIIUpper(scalar) && next.map(isASCIILower) == true)
                if camelBoundary, !current.isEmpty {
                    terms.insert(current)
                    current = ""
                }
            }
            current += String(scalar).lowercased()
        }
        if !current.isEmpty { terms.insert(current) }
        return !terms.isDisjoint(with: secretTerms)
    }

    private static func isASCIIAlpha(_ scalar: UnicodeScalar) -> Bool {
        isASCIILower(scalar) || isASCIIUpper(scalar)
    }

    private static func isASCIIAlphaNumeric(_ scalar: UnicodeScalar) -> Bool {
        isASCIIAlpha(scalar) || (scalar.value >= 48 && scalar.value <= 57)
    }

    private static func isASCIILower(_ scalar: UnicodeScalar) -> Bool {
        scalar.value >= 97 && scalar.value <= 122
    }

    private static func isASCIIUpper(_ scalar: UnicodeScalar) -> Bool {
        scalar.value >= 65 && scalar.value <= 90
    }

    private static func isASCIILowerOrDigit(_ scalar: UnicodeScalar) -> Bool {
        isASCIILower(scalar) || (scalar.value >= 48 && scalar.value <= 57)
    }
}
