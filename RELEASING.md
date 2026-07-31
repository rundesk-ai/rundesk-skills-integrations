# Releasing Rundesk Integration Skills

1. Put the intended package changes and one semantic `manifest.json` version bump in a pull
   request against `main`.
2. Run `python3 -m unittest discover -s tests -v` and wait for the build matrix.
3. Review the manifest, environment precedence, credential redaction, bounded reads, and guarded
   mutations together before merging.
4. Tag the merge commit with the manifest version prefixed by `v` and push the tag.

```sh
version=$(python3 -c 'import json; print(json.load(open("manifest.json"))["version"])')
git tag "v$version" <merge-commit>
git push origin "v$version"
```

The release workflow refuses a mismatched tag, reruns the suite, and creates the GitHub Release.
Never move or reuse a published tag.
