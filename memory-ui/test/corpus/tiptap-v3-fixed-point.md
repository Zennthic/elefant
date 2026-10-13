# Tiptap 3 fixed point

This page is already in the form the editor writes, so re-saving it must give it back byte for byte. It pins the constructs the Tiptap 2 to 3 upgrade touched: tables (column alignment included), task lists, code blocks, images and hard breaks. Task lists are written loose, with a blank line between items, as they were on Tiptap 2.

## Tables

| Left | Center | Right | Plain |
| :--- | :---: | ---: | --- |
| a | **b** | `c` | [d](https://d.example) |
| 1 | 2 | 3 | 4 |

| Name | Value |
| --- | --- |
| no alignment | stays plain |

## Task lists

- [ ] open item

- [x] done item

  - [ ] nested open

  - [x] nested done

## Code block

```python
def hello(name):
    return f"hi {name}"
```

## Image

![diagram|320](../assets/images/diagram.png)

![plain](../assets/images/plain.png)

## Hard break

first line\
second line
