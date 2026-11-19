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
* **Proto manifest**: aapt2 writes the attributes of `<manifest>` in its own order (`package` first), keeps the whitespace
  text nodes of the source with real line and column positions, and writes the source position of every attribute. The builder
  writes the same fields (text nodes by default, `text=False` drops them) with synthetic positions. The tests compare
  elements, attribute names, resource ids, values and value types, not bytes.
* `aapt2 convert --output-format binary` turns the builder's proto manifest into an AXML equal to `make_axml`'s output
  apart from line numbers (checked in `test_aapt2_converts_our_proto_manifest_back_to_axml`).

### Notes for the parser workstream

* The golden AXML uses a UTF-16 string pool (`flags` = 0), as contract 6.7 says. UTF-8 pools exist in the wild; the builder does not write them.
* `0x01010000` (theme) is a reference value (`@0x01030224`, data type `0x01`); the proto side stores it as `@android:style/Theme.Material`
  plus a compiled reference item.
* `package` has no namespace and no resource id; the aapt2-added `platformBuildVersionCode` and `platformBuildVersionName`
  are integers without a resource id.
* Booleans are `0xFFFFFFFF` (true) and `0` (false) in AXML; in the proto manifest they are `true`/`false` text plus a compiled item.
* `make_dex` returns the sorted, de-duplicated union of its string and type arguments (see the module docstring), so
  `strings()` of a fixture is that sorted union.
* The attribute resource ids used by the builders (`fixtures_archives.RES_IDS`) are checked against the attr table of
  `android.jar` by `test_res_ids_match_android_jar` when the SDK is installed.
