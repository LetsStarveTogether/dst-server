# `0x10010000` BBC Document Tree Specification

This page applies the `~/gh/BBC` convention to this repository's orientation directory.

BBC is a tree convention shared by Markdown directories, topic files, and headings, not a numeric filename prefix.

Every BBC-managed node maps to the fixed eight-digit layout `0xD1D2D3FH2H3H4H5`.

## `0x10011111` Name Codes Are Not Heading Codes

Directory names contain only name codes, never heading codes.

`0x100000-orientation` and `0x1100000-reading-model` embed a directory carrier's heading code in its filesystem name.

BBC directory and topic filenames may contain only a one- to four-digit name code, `D1D2D3F`.

Therefore, the six-digit directory name `0x100000-orientation` is invalid.

`0x10-orientation` cannot represent a first-level directory either.

As a name code, `0x10` is first left-padded to `0010`, then restored to the full layout `0x00100000`.

Its `001` directory digits skip empty `D1` and `D2`, violating contiguous, left-aligned numbering.

## `0x10011211` Restore Slots Before Reading Them

Interpret a code by where it is written, restore the standard layout, and then read each slot.

Directories and topic files store only `D1D2D3F` in the filesystem.

Markdown headings use the restored heading code.

An H1 uses the full layout of its directory carrier or topic file.

Headings below H1 fill the logical `H2 H3 H4 H5` slots.

Visible Markdown levels reflect the actual reading structure and do not require empty headings for unused logical slots.

Never append a heading number to a filename code.

This repository keeps orientation, BBC, source snapshots, reading workflows, terminology, and maintenance under `0x1000-orientation`.

It does not create a `0x1100-reading-model` subdirectory solely for these topics.

## `0x10012111` Eight-Digit Layout

A complete BBC layout has eight digits.

```text
0x D1 D2 D3  F-H1 H2 H3 H4 H5
   └───────────╂────────────┘
    Directory  ┃    Heading
           File and H1
```

`D1 D2 D3` describe the directory path from the documentation root.

Directory slots fill from left to right.

One level is `a00`, two levels are `ab0`, and three levels are `abc`.

`F-H1=0` identifies the current directory's README carrier.

`F-H1=1` through `F` identify topic files in that directory.

The same `F-H1` value is the H1 slot for its carrier or topic file.

`H2 H3 H4 H5` describe the logical heading path below H1.

Visible Markdown headings need not expose every logical slot.

BBC does not encode H6.

## `0x10012211` Use Dense, Continuous Numbering

Sibling nodes under one parent increment densely from `1` through `F`.

`0` means that a structural slot is empty, not that it is the zeroth node.

Directory slots cannot skip a level.

Nonzero heading slots in a heading code must be contiguous and left-aligned.

Visible Markdown nesting may stop wherever the reading structure stops.

If a parent needs more than 15 children, split them into another parent directory, topic file, or heading group.

## `0x10013111` Interpret Codes by Location

Read the location of a `0x` string before interpreting it.

| Location | Form | Restoration |
| --- | --- | --- |
| `README.md` | Fixed filename | Use the enclosing directory's full carrier layout with `F-H1=0`. |
| Directory name | One- to four-digit name code | Left-pad to `D1D2D3F`, then append `0000`. |
| Topic filename | One- to four-digit name code | Left-pad to `D1D2D3F`, then append `0000`. |
| Markdown heading | One- to eight-digit heading code | Left-pad to `D1D2D3FH2H3H4H5`. |

Directory and topic filenames cannot use a full heading code with `H2H3H4H5=0000`.

A README does not consume a topic-file number.

Topic files start at `F-H1=1`.

## `0x10013211` Omit Leading Zeros Consistently

Leading zeros may be omitted only consistently within one controlled scope.

A directory name and its README heading share one scope.

A topic filename and the headings in its body share another scope.

The directory and file name codes in this section begin with a nonzero `D1`, so they have no leading zeros to omit.

## `0x10014111` Repository Mapping

| Path | Name code | Full H1 or carrier layout | Purpose |
| --- | --- | --- | --- |
| `0x1000-orientation/README.md` | `0x1000` | `0x10000000` | Orientation directory carrier |
| `0x1000-orientation/0x1001-bbc-encoding.md` | `0x1001` | `0x10010000` | BBC document tree specification |
| `0x1000-orientation/0x1002-source-snapshot.md` | `0x1002` | `0x10020000` | Source snapshot |
| `0x1000-orientation/0x1003-reading-workflows.md` | `0x1003` | `0x10030000` | Reading workflows |
| `0x1000-orientation/0x1004-glossary.md` | `0x1004` | `0x10040000` | Glossary |
| `0x1000-orientation/0x1005-maintenance.md` | `0x1005` | `0x10050000` | Maintenance rules |

## `0x10014211` Expanded Tree

```text
0x10000000  0x1000-orientation/
            └── README.md
                └── # `0x10000000` Orientation and Reading Model
0x10001111          └── ## `0x10001111` Scope and Boundaries

0x10010000          └── 0x1001-bbc-encoding.md
                        └── # `0x10010000` BBC Document Tree Specification
0x10011111                  └── ## `0x10011111` Name Codes Are Not Heading Codes
```

## `0x10015111` Calculate Heading Codes by Filling Slots

The name code of this file, `0x1001-bbc-encoding.md`, is `0x1001`.

It restores to the full layout `0x10010000`.

In that layout, `D1=1`, `D2=0`, and `D3=0` identify the orientation directory.

`F-H1=1` identifies both the directory's first topic file and this page's H1.

The first content group fills `H2=1`, `H3=1`, `H4=1`, and `H5=1` in order.

The resulting heading code is `0x10011111`.

This logical path may appear as one H2 because it is already the smallest useful reading unit.

Later siblings increment the last meaningful logical slot, as in `0x10011211`.

## `0x10015211` Do Not Append Heading Numbers

Writing this page's first H2 as `0x100100001` is invalid because a heading code has at most eight digits.

`0x10010001` is also invalid for this heading.

It parses as `D1=1 D2=1 D3=0 F-H1=1 H2=0 H3=0 H4=0 H5=1`.

That layout creates H5 without H2, H3, or H4.

Instead, fill a continuous logical path such as `0x10011111`.

## `0x10015311` Apply the Same Algorithm to README

The full carrier layout for `README.md` is `0x10000000`.

Its first content group fills a continuous logical path and becomes `0x10001111`.

That group appears as one H2 in the current document.

Because a README uses `F-H1=0`, it consumes no topic-file number.

The first topic file in the same directory remains `0x1001-bbc-encoding.md`.

## `0x10016111` Use README as the Directory Carrier

Every documentation directory should have its own `README.md` as its carrier.

The directory README defines the directory's purpose, boundary, child-page index, and recommended entry point.

It does not carry the body of a specific topic.

Do not make the first topic file perform the directory README's role.

## `0x10016211` Keep One Topic per Topic File

A topic file covers one specific question, reading path, or verifiable maintenance action.

It uses a nonzero `F-H1`.

Its H1 matches the file's full layout.

Text that only explains a directory's purpose, contents, or recommended first page belongs in the directory README.

## `0x10016311` Keep Internal Headings Dense

Heading levels express a file's visible internal structure.

Heading codes fill `H2 H3 H4 H5` continuously.

Visible Markdown retains only levels that carry content.

Sibling headings under one parent increment densely from `1` through `F`.

Never reserve empty numbers for future insertions.

If one parent needs more than 15 sibling headings, create another parent heading or topic file.

## `0x10017111` Add Content by Responsibility

Before adding content, decide whether it describes the directory itself or an independent topic.

Put directory-level content in that directory's `README.md`.

Create or update `0x<name-code>-<kebab-topic>.md` for an independent topic.

For a new heading, find its nearest parent and use the next available hexadecimal sibling number.

Put every heading code in a Markdown code span.

## `0x10017211` Validate the Tree

Run these commands from `dst-scripts/index`.

```bash
rumdl check 0x1000-orientation
git diff --check -- 0x1000-orientation
rg -n '^#{1,5} [^`]*0x[0-9A-Fa-f]+' 0x1000-orientation
```

The third command should produce no output.
