// Local Apple Vision OCR. Never emit full text, names, or identifiers.
import Foundation
import Vision

let terms = ["acne", "rosacea", "eczema", "melasma", "psoriasis", "comedo", "pustule", "papule", "hyperpigmentation", "erythema", "redness", "flaking", "scale", "brown", "pigment", "plaque", "eritema", "descama", "pápula", "pústula", "dermatitis", "tinea", "scabies", "vitiligo", "keratosis", "carcinoma", "melanoma", "脱屑", "脫屑", "痤疮", "泛紅", "痘痘"]
while let line = readLine() {
    var output: [String: Any] = ["path": line, "engine": "Apple Vision VNRecognizeTextRequest", "policy_version": 2, "complete": false]
    do {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["en-US"]
        request.usesLanguageCorrection = false
        let handler = VNImageRequestHandler(url: URL(fileURLWithPath: line), options: [:])
        try handler.perform([request])
        let strings = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string.lowercased() }
        let text = strings.joined(separator: " ")
        output["condition_token_hits"] = terms.filter { text.contains($0) }
        output["text_regions"] = strings.count
        output["complete"] = true
    } catch {
        output["error"] = String(describing: type(of: error))
    }
    if let data = try? JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]), let result = String(data: data, encoding: .utf8) {
        print(result)
        fflush(stdout)
    }
}
