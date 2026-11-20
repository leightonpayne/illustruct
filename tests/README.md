# Molecular fixtures

Native and GUI tests use local coordinate files from the source distribution.

| File             | PDB entry                                   | Use                                    |
| ---------------- | ------------------------------------------- | -------------------------------------- |
| `crambin.pdb`    | [1CRN](https://www.rcsb.org/structure/1CRN) | Small protein, commands, and exports   |
| `hemoglobin.pdb` | [4HHB](https://www.rcsb.org/structure/4HHB) | Multiple protein chains and GUI checks |
| `dna.pdb`        | [1BNA](https://www.rcsb.org/structure/1BNA) | DNA backbone rendering                 |
| `trna.pdb`       | [1EHZ](https://www.rcsb.org/structure/1EHZ) | RNA and modified residues              |

The PDB files carry their scientific citations. Cite the relevant structures and
publications when reusing the data in figures. GUI test reports go in `qa/`.
