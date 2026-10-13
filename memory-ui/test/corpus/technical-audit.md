# Sample Reference Document

A neutral fixture for the Markdown ↔ editor round-trip test. It exercises the
formatting the editor must preserve, without any project-specific content.

## Headings and inline formatting

Paragraphs support **bold**, *italic*, `inline code`, and
[links](https://example.com). One word, one meaning.

### A third-level heading

> A blockquote, to check that block-level quoting survives the round trip.

## Lists

- First bullet
- Second bullet
  - Nested bullet
- Third bullet

1. First step
2. Second step
3. Third step

## A table

| Feature     | Supported | Notes                 |
|-------------|-----------|-----------------------|
| Headings    | yes       | levels 1-3            |
| Tables      | yes       | with a header row     |
| Code blocks | yes       | fenced, with language |

## A code block

```python
def greet(name: str) -> str:
    return f"hello, {name}"
```

## Closing

A final paragraph, followed by a horizontal rule.

---

End of fixture.
