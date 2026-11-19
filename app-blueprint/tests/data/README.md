# Test data

Small, synthetic files for the tests. Nothing here comes from a real app; the package is `com.example.snagdesk`.

## `android/` (golden files made by aapt2)

| File | What it is |
|---|---|
| `AndroidManifest.xml` | the hand-written source: permissions, a launcher activity, a verified https deep link, a service, a receiver, a provider, a framework theme reference |
| `manifest.axml` | the binary manifest (AXML) `aapt2 link` wrote for it (3,924 bytes) |
| `manifest.proto` | the proto manifest (aapt2 `XmlNode`) `aapt2 link --proto-format` wrote for it (3,656 bytes) |

Made with Android SDK build-tools **36.0.0**, `aapt2 version` = `Android Asset Packaging Tool (aapt) 2.20-13193326`, and
`platforms/android-36/android.jar` (the SDK platform whose attribute ids are the ones used):

```
aapt2 link --manifest AndroidManifest.xml -I <sdk>/platforms/android-36/android.jar -o bin.apk
aapt2 link --proto-format --manifest AndroidManifest.xml -I <sdk>/platforms/android-36/android.jar -o proto.apk
# then the single member AndroidManifest.xml of each zip is saved as manifest.axml / manifest.proto
```

Dex files are not golden files: `dexdump` 36.0.0 (same build-tools) read the output of `make_dex` without complaint, and
`test_fixtures_archives.py` runs it again when the tool is present.

### How `fixtures_archives.make_axml` and `make_proto_manifest` relate to the goldens

`make_axml(SNAGDESK_MANIFEST + the four attributes aapt2 adds)` is equal to `manifest.axml` byte for byte except for the
node line numbers (aapt2 writes the line of the source XML, the builder writes the element number). Same string pool
(UTF-16, id attribute names first in id order, the rest sorted), same resource map, same attribute order, same value types.
What differs, and why:

* **aapt2 adds four attributes to `<manifest>`**: `android:compileSdkVersion` (36), `android:compileSdkVersionCodename`
  ("16"), `platformBuildVersionCode` (36) and `platformBuildVersionName` (16, an integer). The builder adds nothing by itself.
* **Line numbers** in node headers (see above).
* **Proto manifest**: aapt2 does not sort attributes by resource id (in the golden `package` comes first, then the others in
  an order of its own, with its four added attributes among them) and records real source positions: line and column on
  elements, text nodes and namespace declarations, and a line only on attributes that have a compiled item (string-valued
  attributes such as `package`, `versionName`, `label` and `name` carry no position at all). The builder keeps the attribute
  order it is given and writes synthetic positions (line = element number + 2, column = 4 x depth), with a line on every
  attribute, so a parser must not rely on a position being present. Whitespace text
  nodes are written by default (`text=False` drops them), as aapt2 keeps those of the source. The tests compare elements,
  attribute names, resource ids, values and value types, not bytes.
* `aapt2 convert --output-format binary` turns the builder's proto manifest into an AXML equal to `make_axml`'s output
  apart from line numbers (checked in `test_aapt2_converts_our_proto_manifest_back_to_axml`).

### Notes for the parser workstream

* The golden AXML uses a UTF-16 string pool (`flags` = 0), as contract 6.7 says. UTF-8 pools exist in the wild: `make_axml(..., utf8=True)`
  writes one (and `attr_size`, `attr_start`, `extra_chunk` write other legal layouts); aapt2 reads all of them, and
  `test_aapt2_reads_the_axml_variants_like_the_default` checks that with the installed tool.
* `0x01010000` (theme) is a reference value (`@0x01030224`, data type `0x01`); the proto side stores it as `@android:style/Theme.Material`
  plus a compiled reference item.
* `package` has no namespace and no resource id; the aapt2-added `platformBuildVersionCode` and `platformBuildVersionName`
  are integers without a resource id.
* Booleans are `0xFFFFFFFF` (true) and `0` (false) in AXML; in the proto manifest they are `true`/`false` text plus a compiled item.
* `make_dex` returns the sorted, de-duplicated union of its string and type arguments (see the module docstring), so
  `strings()` of a fixture is that sorted union.
* The attribute resource ids used by the builders (`fixtures_archives.RES_IDS`) are checked against the attr table of
  `android.jar` by `test_res_ids_match_android_jar` when the SDK is installed.

### Which SDK the tests use, and what they tolerate

The goldens were made with **build-tools 36.0.0 and platform android-36** (above). The tests that run `aapt2` or
`dexdump` pick the highest installed version of each (build-tools and `platforms/android-*` are sorted by version number,
looked up under `ANDROID_HOME`, `ANDROID_SDK_ROOT`, the default SDK folders, then `PATH`) and skip when none is found. They
do not need the SDK that made the goldens:

* `aapt2 link` stamps four attributes into `<manifest>` from the installed SDK (`android:compileSdkVersion`,
  `android:compileSdkVersionCodename`, `platformBuildVersionCode`, `platformBuildVersionName`). The tests drop exactly these
  (the `SDK_STAMPED` constant in `test_fixtures_archives.py`) from both sides before comparing a fresh link with the golden;
  everything else, resource ids included, must still be equal. They also relink with other stamp values
  (`--compile-sdk-version-code 37 --compile-sdk-version-name 17`) and with `--no-compile-sdk-metadata` to prove it.
* The `RES_IDS` check against `android.jar` ignores an attribute that a platform does not have, and fails only on a different id.
* The `aapt2 dump` and `dexdump` checks look for the package line, the version code and the header counts with patterns, not
  for the exact layout of the tool's output.
