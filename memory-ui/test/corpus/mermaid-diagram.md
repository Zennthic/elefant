# Login flow

How a user signs in.

```mermaid
flowchart TD
  A[Log in] --> B[Check password]
  B -->|ok| C[Home]
  B -.->|"fail <3x"| A
```
