# `0x0000` DST Scripts Index

This index maps the Don't Starve Together Lua source tree by runtime system and reading path.

The Markdown tree follows [BBC](https://github.com/WH-2099/BBC).

All `scripts/...` paths and commands are relative to the parent `dst-scripts/` directory.

## `0x1111` Document Model

- The root `README.md` uses the root carrier code `0x0000` in its H1.
- Directory, file, and heading codes must map to the eight-digit BBC layout `0xD1D2D3FH2H3H4H5`.
- Filesystem names contain only the `D1D2D3F` name code; headings record the logical path.
- Every directory has a `README.md` that defines its scope and child-page index.
- Heading depth follows the content and stops without empty placeholder levels.
- Heading sequence numbers remain separate from file-level codes.

## `0x2111` Sections

- [Orientation and Reading Model](0x1000-orientation/README.md)
- [Runtime](0x2000-runtime/README.md)
- [Entities and Actions](0x3000-entity-action/README.md)
- [AI and Animation](0x4000-ai-animation/README.md)
- [World Simulation](0x5000-world-simulation/README.md)
- [Gameplay Systems](0x6000-gameplay-systems/README.md)
- [Frontend, Data, and Tools](0x7000-frontend-data-tools/README.md)
- [Reference](0x8000-reference/README.md)

## `0x3111` Reading Paths

- **Boot:** Follow the `require` order in `scripts/main.lua`, then read `Start` in `scripts/mainfunctions.lua`.
- **Actions:** `Input` passes through `PlayerController` and `CollectActions`.
  `PlayerActionPicker:SortActionList` creates a `BufferedAction`.
  Continue through `PlayerController:DoAction` to `LocoMotor:PushAction`.
  Follow `EntityScript:PushBufferedAction` through `StateGraphInstance:StartAction` to `BufferedAction:Do`.
- **AI and animation:** Start with `StateGraphInstance:StartAction` for presentation and `BrainWrangler:Update` for decisions.
- **World generation:** Trace `DoGenerateWorld` to `WorldGenScreen` and `TheSim:GenerateNewWorld`.
  `GenerateNew` constructs `Level`, then calls `forest_map.Generate` and `BuildStory`.
  `Story:GenerationPipeline` then calls `Story:GenerateNodesFromTasks`.
- **File lookup:** Use [Reference](0x8000-reference/README.md) for complete inventories and catalogs.

## `0x4111` Runtime Map

~~~mermaid
flowchart TD
    A["main.lua loads runtime modules"]
    A --> B["engine invokes mainfunctions.lua Start"]
    A --> M["input.lua Input"]
    B --> C["TheFrontEnd = FrontEnd()"]
    B --> D["require gamelogic"]
    D --> E["load world and assemble entities"]
    E --> F["entityscript.lua EntityScript"]
    F --> L["brain.lua selects longer-term intent"]
    M --> C2["frontend.lua FrontEnd:OnControl"]
    M --> N["playercontroller.lua"]
    N --> O["playeractionpicker.lua CollectActions"]
    O --> P["PlayerActionPicker:SortActionList creates BufferedAction"]
    P --> W["PlayerController:DoAction"]
    W --> X["locomotor.lua PushAction"]
    X --> Q["EntityScript:PushBufferedAction"]
    Q --> R["stategraph.lua routes the action state"]
    R --> S["PerformBufferedAction -> BufferedAction:Do"]
    D --> I["WorldGenScreen"]
    I --> T["TheSim:GenerateNewWorld"]
    T --> J["worldgen_main.lua GenerateNew"]
    J --> Y["map/level.lua Level"]
    J --> U["forest_map.Generate"]
    U --> K["storygen.BuildStory"]
    K --> Z["Story:GenerationPipeline"]
    Z --> V["Story:GenerateNodesFromTasks"]
~~~
