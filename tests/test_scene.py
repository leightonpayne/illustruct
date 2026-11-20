"""ChimeraX structure capture tests."""

import importlib.util
from pathlib import Path
import unittest

import numpy as np


SESSION = globals().get("session")
_spec = importlib.util.spec_from_file_location("illustruct_scene_under_test", Path(__file__).resolve().parents[1] / "src" / "scene.py")
scene = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scene)


@unittest.skipUnless(SESSION is not None, "Run these integration tests inside ChimeraX with --script")
class SceneIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.created = []
        self.camera = SESSION.main_view.camera
        self.camera_position = self.camera.position

    def tearDown(self):
        SESSION.main_view.camera = self.camera
        self.camera.position = self.camera_position
        for model in self.created:
            if not model.deleted:
                SESSION.models.close([model])

    def atom(self, model, residue, name, element, coord):
        atom = model.new_atom(name, element)
        residue.add_atom(atom)
        atom.coord = coord
        atom.display = True
        atom.color = (60, 120, 180, 255)
        return atom

    def protein(self, numbers=(1, 2, 3, 4), chain="A"):
        from chimerax.atomic import AtomicStructure

        model = AtomicStructure(SESSION, name="Scene protein", auto_style=False, log_info=False)
        self.created.append(model)
        previous = None
        for index, number in enumerate(numbers):
            residue = model.new_residue("ALA", chain, number)
            residue.ribbon_display = False
            residue.ribbon_color = (80, 160, 100, 255)
            x = index * 3.8
            atoms = {}
            for name, element, offset in (("N", "N", (-1.2, 0, 0)), ("CA", "C", (0, 0, 0)),
                                         ("C", "C", (1.2, 0, 0)), ("O", "O", (1.2, 1, 0)),
                                         ("CB", "C", (0, 1.5, 0))):
                atoms[name] = self.atom(model, residue, name, element, (x + offset[0], offset[1], offset[2]))
            for a, b in (("N", "CA"), ("CA", "C"), ("C", "O"), ("CA", "CB")):
                model.new_bond(atoms[a], atoms[b])
            if previous is not None:
                model.new_bond(previous, atoms["N"])
            previous = atoms["C"]
        SESSION.models.add([model])
        return model

    def capture(self, model, **options):
        return scene.capture_scene(SESSION, model_ids=[tuple(model.id)], **options)

    def nucleic_sample(self, filename="dna.pdb"):
        path = Path(__file__).with_name(filename)
        models, _ = SESSION.open_command.open_data(str(path), format="pdb")
        SESSION.models.add(models)
        self.created.extend(models)
        model = models[0]
        model.residues.ribbon_displays = False
        model.atoms.displays = True
        model.atoms.hides = 0
        model.atoms.selected = False
        return model

    def test_visibility_and_selection_control_structure_capture(self):
        model = self.protein()
        model.atoms.displays = False
        model.residues.ribbon_displays = True
        model.residues.ribbon_colors = (20, 100, 140, 128)
        result = self.capture(model)
        self.assertEqual(result["atom_count"], 20)
        part, = result["parts"]
        self.assertEqual((part["kind"], part["color"]), ("protein", "#14648c"))
        self.assertAlmostEqual(part["opacity"], 128 / 255)
        model.display = False
        self.assertEqual(self.capture(model)["atom_count"], 0)
        model.atoms[0].selected = True
        part, = self.capture(model, scope="selection")["parts"]
        np.testing.assert_array_equal(part["atom_indices"], [0])

    def test_explicit_atoms_override_filters_without_changing_selection(self):
        from chimerax.atomic import Atoms
        from chimerax.geometry import Places, translation

        model, other = self.protein(), self.protein(chain="B")
        wanted = Atoms([model.atoms[1], model.atoms[4], model.atoms[6]])
        hydrogen = self.atom(model, model.residues[0], "H", "H", (0, 0, 1))
        water = model.new_residue("HOH", "W", 1)
        oxygen = self.atom(model, water, "O", "O", (0, 0, 2))
        explicit = Atoms(list(wanted) + [hydrogen, oxygen])
        model.atoms.displays = False
        model.display = False
        placements = [translation((10, 20, 30)), translation((-20, 5, 10))]
        model.positions = Places(placements)
        model.display_positions = np.array([True, False])
        model.atoms.selected = False
        other.atoms.selected = False
        wanted[1].selected = True
        other.atoms[0].selected = True
        selections = [structure.atoms.selected.copy() for structure in (model, other)]
        expected_indices = set(model.atoms.indices(wanted))

        result = scene.capture_scene(SESSION, atoms=explicit, scope="selection",
                                     model_ids=[tuple(other.id)], selected_domain=True)
        self.assertEqual(result["atom_count"], 6)
        self.assertEqual({part["model_id"] for part in result["parts"]}, {tuple(model.id)})
        for instance, placement in enumerate(placements):
            parts = [part for part in result["parts"] if part["instance_index"] == instance]
            self.assertEqual({int(i) for part in parts for i in part["atom_indices"]}, expected_indices)
            for part in parts:
                matrix = placement.matrix
                coords = model.atoms.coords[part["atom_indices"]]
                np.testing.assert_allclose(part["coords"], coords @ matrix[:, :3].T + matrix[:, 3])
        for structure, selected in zip((model, other), selections):
            np.testing.assert_array_equal(structure.atoms.selected, selected)

    def multicolored_protein(self, colors=None):
        colors = colors or ((20, 100, 140, 255), (187, 197, 212, 255), (184, 221, 165, 255))
        model = self.protein(numbers=tuple(range(1, len(colors) * 2 + 1)))
        model.atoms.displays = False
        model.residues.ribbon_displays = True
        for index, residue in enumerate(model.residues):
            residue.ribbon_color = colors[index // 2]
        return model

    def test_color_regions_and_selected_domains_preserve_atom_membership(self):
        blue, grey = (20, 100, 140, 255), (187, 197, 212, 255)
        model = self.multicolored_protein((blue, grey, blue))
        result = self.capture(model)
        self.assertEqual({part["color"] for part in result["parts"]}, {"#14648c", "#bbc5d4"})
        blue_part = next(p for p in result["parts"] if p["color"] == "#14648c")
        self.assertEqual([len(segment) for segment in blue_part["segments"]], [2, 2])
        model.residues[1].find_atom("CB").selected = True
        model.residues[3].atoms.selected = True
        selected_before = model.atoms.selected.copy()
        parts = self.capture(model, selected_domain=True)["parts"]
        self.assertEqual(sum(p["selected_domain"] for p in parts), 2)
        indices = [i for p in parts for i in p["atom_indices"]]
        self.assertEqual(sorted(indices), list(range(30)))
        np.testing.assert_array_equal(model.atoms.selected, selected_before)

    def test_component_type_uses_complete_residue_when_ligand_is_partly_hidden_or_selected(self):
        model = self.protein(numbers=(1, 2))
        ligand = model.new_residue("LIG", "A", 100)
        first = self.atom(model, ligand, "C1", "C", (20, 0, 0))
        second = self.atom(model, ligand, "O1", "O", (21, 0, 0))
        model.new_bond(first, second)
        chloride = model.new_residue("CL", "A", 101)
        self.atom(model, chloride, "CL", "Cl", (25, 0, 0))
        complete = self.capture(model)
        ligand_part = next(part for part in complete["parts"] if part["category"] == "ligands")
        self.assertEqual({part["category"] for part in complete["parts"]}, {"proteins", "ligands", "ions"})
        second.display = False
        partial = next(part for part in self.capture(model)["parts"] if part["id"] == ligand_part["id"])
        self.assertEqual(len(partial["coords"]), 1)
        self.assertEqual(partial["category"], "ligands")
        first.selected = True
        selected = self.capture(model, scope="selection")["parts"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["id"], ligand_part["id"])
        self.assertEqual(selected[0]["category"], "ligands")
        self.assertFalse(second.display)

    def test_dna_backbones_include_terminal_sugars_and_keep_bond_gaps(self):
        model = self.nucleic_sample()
        before = (model.atoms.coords.copy(), model.atoms.hides.copy(), model.atoms.selected.copy())
        parts = [part for part in self.capture(model)["parts"] if part["kind"] == "nucleic"]
        self.assertEqual(len(parts), 2)
        for part in parts:
            residues = [residue for residue in model.residues
                        if residue.chain_id == part["chain_id"] and residue.find_atom("C4'") is not None]
            self.assertEqual(len(residues), 12)
            self.assertIsNone(residues[0].find_atom("P"))
            self.assertEqual([len(segment) for segment in part["segments"]], [12])
            np.testing.assert_allclose(part["segments"][0], [residue.find_atom("C4'").coord for residue in residues])
        for expected, actual in zip(before, (model.atoms.coords, model.atoms.hides, model.atoms.selected)):
            np.testing.assert_array_equal(expected, actual)

        residues = [residue for residue in model.residues
                    if residue.chain_id == "A" and residue.find_atom("C4'") is not None]
        bond, = residues[5].bonds_between(residues[6])
        bond.delete()
        part = next(part for part in self.capture(model)["parts"] if part["chain_id"] == "A")
        self.assertEqual([len(segment) for segment in part["segments"]], [6, 6])

    def test_real_trna_backbone_includes_modified_residues_and_pseudouridine(self):
        model = self.nucleic_sample("trna.pdb")
        parts = [part for part in self.capture(model)["parts"] if part["kind"] == "nucleic"]
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["residue_count"], 76)
        anchors = np.concatenate(parts[0]["segments"])
        self.assertEqual(len(anchors), 76)
        psu = next(residue for residue in model.residues if residue.name == "PSU")
        self.assertTrue(any(np.allclose(anchor, psu.find_atom("C4'").coord) for anchor in anchors))
        self.assertTrue(np.isfinite(anchors).all())

    def test_backbone_respects_exact_atom_selection_without_broadening_it(self):
        model = self.nucleic_sample()
        residue = model.residues[0]
        anchor = residue.find_atom("C4'")
        anchor.selected = True
        before = model.atoms.selected.copy()
        captured = self.capture(model, scope="selection")
        self.assertEqual(captured["atom_count"], 1)
        np.testing.assert_array_equal(captured["parts"][0]["segments"][0], [anchor.coord])
        np.testing.assert_array_equal(before, model.atoms.selected)
        anchor.selected = False
        residue.find_atom("N3").selected = True
        captured = self.capture(model, scope="selection")
        self.assertEqual(captured["atom_count"], 1)
        self.assertEqual(captured["parts"][0]["segments"], [])

    def test_nucleic_traces_transform_with_instances_and_are_detached(self):
        from chimerax.geometry import translation, rotation, Places

        model = self.nucleic_sample()
        original = model.atoms.coords.copy()
        placements = [translation((10, 20, 30)) * rotation((0, 0, 1), 90), translation((-20, 5, 10))]
        model.positions = Places(placements)
        parts = [part for part in self.capture(model)["parts"] if part["kind"] == "nucleic" and part["chain_id"] == "A"]
        self.assertEqual(len(parts), 2)
        local = np.asarray([residue.find_atom("C4'").coord for residue in model.residues
                            if residue.chain_id == "A" and residue.find_atom("C4'") is not None])
        for part in parts:
            matrix = placements[part["instance_index"]].matrix
            np.testing.assert_allclose(part["segments"][0], local @ matrix[:, :3].T + matrix[:, 3], rtol=1e-6)
            captured_coords = part["coords"].copy()
            part["segments"][0][:] = -999
            np.testing.assert_array_equal(part["coords"], captured_coords)
        np.testing.assert_array_equal(model.atoms.coords, original)
        model.display_positions = np.array([False, True])
        visible = [part for part in self.capture(model)["parts"] if part["kind"] == "nucleic"]
        self.assertEqual(len(visible), 2)
        self.assertTrue(all(part["instance_index"] == 1 for part in visible))

    def test_parent_model_transforms_and_visibility_are_included(self):
        from chimerax.core.models import Model
        from chimerax.geometry import translation

        model = self.protein()
        parent = Model("Transformed group", SESSION)
        self.created.append(parent)
        SESSION.models.add([parent])
        parent.add([model])
        parent.position = translation((100, 0, 0))
        model.position = translation((0, 20, 0))
        result = self.capture(model)
        np.testing.assert_allclose(result["parts"][0]["coords"], model.atoms.scene_coords)
        np.testing.assert_allclose(result["parts"][0]["coords"][0], [98.8, 20, 0])
        parent.display = False
        self.assertEqual(self.capture(model)["atom_count"], 0)


if SESSION is not None and not globals().get("_ILLUSTRUCT_TEST_DISCOVERY", False):
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SceneIntegrationTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError("Native scene bridge integration tests failed.")
    print("ILLUSTRUCT_SCENE_TESTS_OK")
elif __name__ == "__main__":
    unittest.main()
