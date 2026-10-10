//! Contract vectors for ikigai-python (ledger #1034), computed by ikigai-core itself.
//!
//! Not built in ikigai-python (which has no Cargo project, on purpose: devtools' drift
//! scans would read one as a consumer). To regenerate, put this file at `src/main.rs`
//! of a scratch cargo project whose `[dependencies]` are
//!
//!     ikigai-core = "=0.1.92"
//!     ikigai-vocab = "=0.1.92"
//!     serde_json = "1"
//!
//! then, from the ikigai-python checkout,
//!
//!     python -m tests.contract_cases | (cd <scratch> && cargo run -q --release) \
//!         > tests/fixtures/contract_vectors.json
//!
//! It reads the JSON array `tests/contract_cases.py` prints and adds, per case, what
//! core computes:
//! - a door case: `turtle` (`ikigai_vocab::to_turtle` of the description, deserialized
//!   exactly as a host deserializes this peer's JSON Meta face) and `actions` (per
//!   `Description::action_specs`, the `match_iri` for the door's pattern and the
//!   `contract_iri`);
//! - a contract case: `contract_iri` of the one `ActionSpec`, Meta included;
//! - a match case: `match_iri` of the pattern under every verb;
//! - a parse case: `parse_match_iri` and `parse_contract_iri` of the IRI in `pattern`.
use ikigai_core::{match_iri, parse_contract_iri, parse_match_iri, ActionSpec, Description, Verb};
use serde_json::{json, Value};
use std::io::Read;

fn main() {
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    let cases: Vec<Value> = serde_json::from_str(&input).unwrap();
    let mut out = Vec::new();
    for mut case in cases {
        let pattern = case["pattern"].as_str().unwrap().to_string();
        if case["parse"].as_bool() == Some(true) {
            case["parsed_match"] = match parse_match_iri(&pattern) {
                Some((v, p)) => json!([format!("{v:?}"), p]),
                None => Value::Null,
            };
            case["parsed_contract"] = match parse_contract_iri(&pattern) {
                Some((id, v, d)) => json!([id, format!("{v:?}"), d.to_string()]),
                None => Value::Null,
            };
        } else if case["contract"].as_bool() == Some(true) {
            let id = case["description"]["id"].as_str().unwrap().to_string();
            let spec: ActionSpec =
                serde_json::from_value(case["description"]["actions"][0].clone()).unwrap();
            case["contract_iri"] = json!(spec.contract_iri(&id));
        } else if case["match"].as_bool() == Some(true) {
            let verbs = [Verb::Source, Verb::Sink, Verb::Exists, Verb::Delete, Verb::Meta];
            let iris: Vec<Value> = verbs
                .iter()
                .map(|v| json!({"verb": format!("{v:?}"), "match": match_iri(*v, &pattern)}))
                .collect();
            case["matches"] = json!(iris);
        } else {
            let d: Description = serde_json::from_value(case["description"].clone()).unwrap();
            let actions: Vec<Value> = d
                .action_specs()
                .iter()
                .map(|a| {
                    json!({
                        "verb": format!("{:?}", a.verb),
                        "match": match_iri(a.verb, &pattern),
                        "contract": a.contract_iri(&d.id),
                    })
                })
                .collect();
            case["turtle"] = json!(ikigai_vocab::to_turtle(&d));
            case["actions"] = json!(actions);
        }
        out.push(case);
    }
    println!("{}", serde_json::to_string_pretty(&out).unwrap());
}
