# Profiles

A profile maps one game's light names onto controls on your board. Light names
come from the game, so a profile is per-game, but it is not per-board: naming
controls rather than LED indexes means the firmware resolves them to whatever
LEDs that board actually has.

`example.json` is illustrative only. Its names are made up, and no real game
reports exactly those.

## Capturing one

Start the game under spice2x with the API enabled, then:

```
hlp-spice2x --port 1337 --list-lights
```

That prints every light and its current value. Pressing buttons and watching
the values change is the quickest way to work out which name is which.

Then generate a skeleton with every light listed:

```
hlp-spice2x --port 1337 --write-profile mygame.json
```

Fill in the `button` field for the lights you want and leave the rest empty.
Check the result without touching a board:

```
hlp-spice2x --port 1337 --profile mygame.json --dry-run
```

A dry run has no board, so it shows an `index` entry as the control and index
you wrote, such as `Up[1]`, rather than the light that would end up lit. Which
light that is only becomes knowable once a board is attached.

## Boards that give one control several lights

Some layouts wire two buttons to the same control, such as a second Up. Naming
the control lights all of them. To light just one, add an `index`:

```json
"Extra Up": { "button": "UP", "index": 1, "colour": "0000FF" }
```

The index counts that control's lights in the order the board's own light table
lists them, starting at 0. Running the bridge prints which controls have more
than one light, which is the quickest way to find out whether yours do.

## Contributing

Pull requests adding a profile are welcome. Please name the file after the
game, and note in the PR which game and version you captured it from, since
light names can change between releases.
