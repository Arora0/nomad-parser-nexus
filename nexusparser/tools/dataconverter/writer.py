#
# Copyright The NOMAD Authors.
#
# This file is part of NOMAD. See https://nomad-lab.eu for further info.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""The writer class for writing a Nexus file in accordance with a given NXDL."""

import copy
import logging
import sys
import xml.etree.ElementTree as ET

import h5py
import numpy as np

from nexusparser.tools.dataconverter import helpers
from nexusparser.tools import nexus

logger = logging.getLogger(__name__)  # pylint: disable=C0103
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler(sys.stdout))


def does_path_exist(path, h5py_obj) -> bool:
    """Returns true if the requested path exists in the given h5py object."""
    try:
        h5py_obj[helpers.convert_data_dict_path_to_hdf5_path(path)]  # pylint: disable=W0106
        return True
    except KeyError:
        return False


def is_not_data_empty(value) -> bool:
    """Returns True if value is an Numpy array or not None."""
    if isinstance(value, np.ndarray) or value is not None:
        return True
    return False


def get_namespace(element) -> str:
    """Extracts the namespace for elements in the NXDL"""
    return element.tag[element.tag.index("{"):element.tag.rindex("}") + 1]


def handle_dicts_entries(data, grp, entry_name):
    """Handke function for dictionaries found as value of the nexus file.

Several cases can be encoutered:
- Internal links
- External links
- Virtual data set
"""
    if 'internal_link' in data.keys():
        grp[entry_name] = h5py.SoftLink(
            helpers.convert_data_dict_path_to_hdf5_path(data['internal_link']))
    elif 'slice_column' in data.keys():
        physical_dataset = h5py.File(data['file_link'],
                                     'r')[data['external_link']]
        if len(data['slice_row']) == 2:
            length = data['slice_row'][1] - data['slice_row'][0]
        else:
            length = 1
        row_and_col_shape = {}
        row_and_col_shape['start_row'] = data['slice_row'][0]
        row_and_col_shape['start_col'] = data['slice_column'][0]
        if len(data['slice_row']) == 2:
            row_and_col_shape['end_row'] = data['slice_row'][1]
        else:
            row_and_col_shape['end_row'] = data['slice_row'][0] + 1
        if len(data['slice_column']) == 2:
            row_and_col_shape['end_col'] = data['slice_column'][1]
        else:
            row_and_col_shape['end_col'] = data['slice_column'][0] + 1
        layout = h5py.VirtualLayout(shape=(length,), dtype=np.float64)
        vsource = h5py.VirtualSource(data['file_link'],
                                     data['external_link'],
                                     shape=(physical_dataset.shape[0], physical_dataset.shape[1])
                                     )[row_and_col_shape['start_row']:row_and_col_shape['end_row'],
                                       row_and_col_shape['start_col']:row_and_col_shape['end_col']
                                       ]
        layout[:] = vsource
        grp.create_virtual_dataset(entry_name, layout)
    elif 'external_link' in data.keys():
        grp[entry_name] = h5py.ExternalLink(data['file_link'],
                                            data['external_link']
                                            )
    elif 'external_links_to_concatenate' in data.keys():
        total_length = 0
        sources = []
        for index, source_file in enumerate(data['file_links']):
            dataset_entry_key = data['external_links_to_concatenate'][index]
            lenght = h5py.File(source_file, 'r')[data['external_links_to_concatenate'][index]
                                                 ].shape
            vsource = h5py.VirtualSource(source_file, dataset_entry_key, shape=lenght)
            total_length += vsource.shape[0]
            sources.append(vsource)
        layout = h5py.VirtualLayout(shape=total_length,
                                    dtype=np.float64)
        offset = 0
        for vsource in sources:
            length = vsource.shape[0]
            layout[offset:offset + length] = vsource
            offset += length
        grp.create_virtual_dataset(entry_name, layout, fillvalue=0)

    return grp[entry_name]


class Writer:
    """The writer class for writing a Nexus file in accordance with a given NXDL.

    Args:
        data (dict): Dictionary containing the data to convert.
        nxdl_path (str): Path to the nxdl file to use during conversion.
        output_path (str): Path to the output Nexus file.

    Attributes:
        data (dict): Dictionary containing the data to convert.
        nxdl_path (str): Path to the nxdl file to use during conversion.
        output_path (str): Path to the output Nexus file.
        output_nexus (h5py.File): The h5py file object to manipulate output file.
        nxdl_data (dict): Stores xml data from given nxdl file to use during conversion.
        nxs_namespace (str): The namespace used in the NXDL tags. Helps search for XML children.
    """

    def __init__(self, data: dict = None, nxdl_path: str = None, output_path: str = None):
        """Constructs the necessary objects required by the Writer class."""
        self.data = data
        self.nxdl_path = nxdl_path
        self.output_path = output_path
        self.output_nexus = h5py.File(self.output_path, "w")
        self.nxdl_data = ET.parse(self.nxdl_path).getroot()
        self.nxs_namespace = get_namespace(self.nxdl_data)

    def __nxdl_to_attrs(self, path: str = '/') -> dict:
        """
        Return a dictionary of all the attributes at the given path in the NXDL and
        the required attribute values that were requested in the NXDL from the data.
        """
        nxdl_path = helpers.convert_data_converter_dict_to_nxdl_path(path)
        elem = copy.deepcopy(nexus.get_node_at_nxdl_path(nxdl_path, elem=self.nxdl_data))
        if elem is None:
            raise Exception(f"Attributes were not found for {path}. "
                            "Please check this entry in the template dictionary.")

        # Remove the name attribute as we only use it to name the HDF5 entry
        if "name" in elem.attrib.keys():
            del elem.attrib["name"]

        # Fetch values for required attributes requested by the NXDL
        for attr_name in elem.findall(f"{self.nxs_namespace}attribute"):
            elem.attrib[attr_name.get('name')] = self.data[f"{path}/@{attr_name.get('name')}"] or ''

        return elem.attrib

    def ensure_and_get_parent_node(self, path: str, undocumented_paths) -> h5py.Group:
        """Returns the parent if it exists for a given path else creates the parent group."""
        parent_path = path[0:path.rindex('/')] or '/'
        parent_path_hdf5 = helpers.convert_data_dict_path_to_hdf5_path(parent_path)
        parent_undocumented_paths = [path[0:path.rindex("/")] or "/" for path in undocumented_paths]
        if not does_path_exist(parent_path, self.output_nexus):
            parent = self.ensure_and_get_parent_node(parent_path, parent_undocumented_paths)
            grp = parent.create_group(parent_path_hdf5)
            if path not in undocumented_paths:
                attrs = self.__nxdl_to_attrs(parent_path)
                if attrs is not None:
                    grp.attrs['NX_class'] = attrs["type"]
            return grp
        return self.output_nexus[parent_path_hdf5]

    def write(self):
        """Writes the Nexus file with previously validated data from the reader with NXDL attrs."""
        for path, value in self.data.items():
            try:
                if path[path.rindex('/') + 1:] == '@units':
                    continue

                entry_name = helpers.get_name_from_data_dict_entry(path[path.rindex('/') + 1:])
                data = value if is_not_data_empty(value) else "NOT_PROVIDED"

                if entry_name[0] != "@":
                    grp = self.ensure_and_get_parent_node(path, self.data.undocumented.keys())

                    if isinstance(data, dict):
                        dataset = handle_dicts_entries(data, grp, entry_name)
                    else:
                        dataset = grp.create_dataset(entry_name, data=data)
                    units_key = f"{path}/@units"
                    if units_key in self.data.keys() and self.data[units_key] is not None:
                        dataset.attrs["units"] = self.data[units_key]
                    else:
                        dataset.attrs["units"] = "NOT_PROVIDED"
                else:
                    dataset = self.ensure_and_get_parent_node(path, self.data.undocumented.keys())
                    dataset.attrs[entry_name[1:]] = data
            except Exception as exception:
                raise Exception(f"Unkown error occured writing the path: {path} "
                                f"with the following message: {str(exception)}")

        self.output_nexus.close()
