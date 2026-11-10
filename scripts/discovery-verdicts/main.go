// Command discovery-verdicts records how livepeer/node judges each edge case in
// tests/unit/discovery_verdicts.json, so the engine's reader is held to node's answers (D63).
//
// It validates the way discovery/schema_test.go does at the pinned node commit: the vendored schema,
// santhosh-tekuri/jsonschema/v6 with AssertFormat, numbers decoded with UseNumber, built with the Go
// version node's go.mod names (go.mod here pins both). Go is needed only when the pin moves:
//
//	go -C scripts/discovery-verdicts run .          # rewrite every verdict
//	go -C scripts/discovery-verdicts run . -check   # exit 1 when a recorded verdict differs
//
// Each case is a field and a raw JSON value. The document is one entry with one runner, whose required
// fields are fixed, and the case's value in place of that field (or of the entry's address).
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

type verdictCase struct {
	Field    string `json:"field"`
	Value    string `json:"value"`
	Valid    bool   `json:"valid"`
	Document string `json:"document"` // what node validated; the Python test builds the same text
}

type verdicts struct {
	Validator string        `json:"validator"`
	Cases     []verdictCase `json:"cases"`
}

// Document builds the case's document. tests/unit/test_discovery_schema.py builds the same text.
func Document(c verdictCase) string {
	address := `"https://orch.example/deployment"`
	fields := [][2]string{{"url", `"https://orch.example/app"`}, {"app", `"example"`}, {"observed_at", `"2026-10-22T18:05:00Z"`}}
	switch parent, child, nested := strings.Cut(c.Field, "."); {
	case c.Field == "address":
		address = c.Value
	case nested:
		fields = append(fields, [2]string{parent, `{"` + child + `":` + c.Value + `}`})
	default:
		replaced := false
		for i := range fields {
			if fields[i][0] == c.Field {
				fields[i][1], replaced = c.Value, true
			}
		}
		if !replaced {
			fields = append(fields, [2]string{c.Field, c.Value})
		}
	}
	parts := make([]string, len(fields))
	for i, f := range fields {
		parts[i] = `"` + f[0] + `":` + f[1]
	}
	return `[{"address":` + address + `,"runners":[{` + strings.Join(parts, ",") + `}]}]`
}

func valid(schema *jsonschema.Schema, document string) (bool, error) {
	decoder := json.NewDecoder(strings.NewReader(document))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return false, err
	}
	if err := decoder.Decode(&value); err != io.EOF {
		return false, fmt.Errorf("trailing data in %s", document)
	}
	return schema.Validate(value) == nil, nil
}

func main() {
	root := flag.String("root", "../..", "repository root")
	check := flag.Bool("check", false, "exit 1 when a recorded verdict differs, and write nothing")
	flag.Parse()
	compiler := jsonschema.NewCompiler()
	compiler.AssertFormat()
	schema, err := compiler.Compile(filepath.Join(*root, "src/livepeer_builder/contracts/discovery.schema.json"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	path := filepath.Join(*root, "tests/unit/discovery_verdicts.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	var file verdicts
	if err := json.Unmarshal(raw, &file); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	changed := 0
	for i, c := range file.Cases {
		document := Document(c)
		ok, err := valid(schema, document)
		if err != nil {
			fmt.Fprintf(os.Stderr, "case %d (%s %s): %v\n", i, c.Field, c.Value, err)
			os.Exit(2)
		}
		if ok != c.Valid {
			changed++
			fmt.Printf("%s %s: node says valid=%v\n", c.Field, c.Value, ok)
		}
		if document != c.Document {
			changed++
		}
		file.Cases[i].Valid, file.Cases[i].Document = ok, document
	}
	file.Validator = "github.com/santhosh-tekuri/jsonschema/v6 v6.0.3 with AssertFormat, " + runtime.Version()
	if *check {
		if changed > 0 {
			os.Exit(1)
		}
		return
	}
	var out bytes.Buffer
	encoder := json.NewEncoder(&out)
	encoder.SetEscapeHTML(false)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(file); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	if err := os.WriteFile(path, out.Bytes(), 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	fmt.Printf("%d cases, %d verdicts changed\n", len(file.Cases), changed)
}
