"""This tool is reading the xml file

"""
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

import re
import os
import os.path
import sys
from typing import Dict, Any
import xml.etree.ElementTree as ET
import numpy as np
from nomad.utils import strip
from nomad.metainfo import (
    Section, Package, SubSection, Definition, Datetime, Bytes, Unit, MEnum, Quantity)
from nomad.datamodel import EntryArchive
from nomad.metainfo.elasticsearch_extension import Elasticsearch
from nexusparser.tools import nexus

# URL_REGEXP from
# https://stackoverflow.com/questions/3809401/what-is-a-good-regular-expression-to-match-a-url
URL_REGEXP = re.compile(r'(https?:\/\/(www\.)?[-a-zA-Z0-9@:%._\+~#=]'
                        r'{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_\+.~#?&//=]*))')
XML_NAMESPACES = {'nx': 'http://definition.nexusformat.org/nxdl/3.1'}

# TO DO the validation still show some problems. Most notably there are a few higher
# dimensional fields with non number types, which the metainfo does not support
VALIDATE = False
CURRENT_PACKAGE: Package = None
_definition_sections: Dict[str, Section] = dict()
_XML_PARENT_MAP: Dict[ET.Element, ET.Element] = None
_NX_DOC_BASE = 'https://manual.nexusformat.org/classes'
_NX_TYPES = {  # Primitive Types,  'ISO8601' is the only type not defined here
    'NX_COMPLEX': np.dtype(np.float64),
    'NX_FLOAT': np.dtype(np.float64),
    'NX_CHAR': str,
    'NX_BOOLEAN': bool,
    'NX_INT': np.dtype(np.int64),
    'NX_UINT': np.dtype(np.uint64),
    'NX_NUMBER': np.dtype(np.number),
    'NX_POSINT': np.dtype(np.uint64),
    'NX_BINARY': Bytes,
    'NX_DATE_TIME': Datetime}


def to_camel_case(snake_str: str, upper: bool = False):
    """Take as input a snake case variable and return a camel case one

"""
    components = snake_str.split('_')

    if upper:
        return ''.join(f'{x[0].upper()}{x[1:]}' for x in components)

    return components[0] + ''.join(f'{x[0].upper()}{x[1:]}' for x in components[1:])


def nx_documenation_url(xml_node: ET.Element, nx_type: str):
    """Get documentation url

"""
    anchor_segments = []
    if nx_type != 'class':
        anchor_segments.append(nx_type)

    while xml_node is not None:
        xml_parent = xml_node
        if 'name' in xml_node.attrib:
            anchor_segments.append(xml_node.attrib['name'].replace('_', '-'))
        else:
            anchor_segments.append(xml_node.attrib['type'][2:].replace('_', '-'))

        xml_node = _XML_PARENT_MAP.get(xml_node)

    anchor = "-".join([name.lower() for name in reversed(anchor_segments)])
    nx_package = xml_parent.get('nxdl_base').split('/')[-1]
    doc_url = f'{_NX_DOC_BASE}/{nx_package}/{anchor_segments[-1]}.html#{anchor}'
    return doc_url


def get_or_create_section(name: str, **kwargs) -> Section:
    """Returns the 'existing' metainfo section for a given top-level nexus base-class name.

    This function ensures that sections for these base-classes are only created one.
    This allows to access the metainfo section even before it is generated from the base-class
    nexus definition.

"""
    if name in _definition_sections:
        section = _definition_sections[name]
        section.more.update(**kwargs)
        return section

    section = Section(validate=VALIDATE, name=name, **kwargs)
    CURRENT_PACKAGE.section_definitions.append(section)
    _definition_sections[section.name] = section

    return section


def get_enum(xml_node: ET.Element):
    """Get the enumeration field from xml node

"""
    enumeration = xml_node.find('nx:enumeration', XML_NAMESPACES)
    if enumeration is not None:
        enum_values = []
        for enum_value in enumeration.findall('nx:item', XML_NAMESPACES):
            enum_values.append(enum_value.attrib['value'])
        return MEnum(*enum_values)
    return None


def add_common_properties_helper(base_section, definition):
    """Define definition var from base_section var

"""
    if base_section.description:
        definition.description = base_section.description
    if base_section.deprecated:
        definition.deprecated = base_section.deprecated
    if base_section.more:
        definition.more.update(**base_section.more)


def get_nexus_category(xml_node: ET.Element):
    '''
    retrieves the category from the root element
    '''
    elem = xml_node
    parent = elem
    while parent is not None:
        elem = parent
        parent = _XML_PARENT_MAP.get(elem)
    return elem.attrib['category']


def add_common_properties(xml_node: ET.Element, definition: Definition):
    '''
    Adds general metainfo definition properties (e.g. deprecated, docs, optional, ...)
    from the given nexus XML node to the given metainfo definition.
    '''
    nx_kind = definition.more.get('nx_kind')
    nx_category = get_nexus_category(xml_node)
    xml_attrs = xml_node.attrib

    # Read properties from potential base section. Those are not inherited, but we
    # duplicate them for a nicer presentation
    if isinstance(definition, Section) and definition.base_sections:
        base_section = definition.base_sections[0]
        add_common_properties_helper(base_section, definition)
    links = []
    if nx_kind is not None:
        doc_url = nx_documenation_url(xml_node, nx_kind)
        if doc_url:
            links.append(doc_url)

    doc = xml_node.find('nx:doc', XML_NAMESPACES)
    if doc is not None and doc.text is not None:
        definition.description = strip(doc.text)
        for match in URL_REGEXP.findall(definition.description):
            links.append(match[0])

    if links:
        definition.links = links

    if 'deprecated' in xml_attrs:
        definition.deprecated = xml_attrs['deprecated']

    definition.more['nx_optional'] = xml_attrs.get(
        'optional', nx_category == 'base')

    if 'minOccurs' in xml_attrs:
        definition.more['nx_min_occurs'] = xml_attrs['minOccurs']
    if 'maxOccurs' in xml_attrs:
        definition.more['nx_max_occurs'] = xml_attrs['maxOccurs']
    if 'required' in xml_attrs:
        definition.more['nx_required'] = xml_attrs['required']
    if 'recommended' in xml_attrs:
        definition.more['nx_recommended'] = xml_attrs['recommended']

    if 'category' in xml_attrs:
        definition.more['nx_category'] = xml_attrs['category']

    # TO DO there are probably even more nxdl attributes?


def add_attributes(xml_node: ET.Element, section: Section):
    '''
    Adds quantities for all attributes in the given nexus XML node to the given
    section.
    '''
    for attribute in xml_node.findall('nx:attribute', XML_NAMESPACES):
        attribute_section = create_attribute_section(attribute, section)

        name = attribute.attrib["name"]
        max_occurs = attribute.attrib.get('maxOccurs', '0')
        repeats = any(name_char.isupper()
                      for name_char in name) or max_occurs == 'unbounded' or int(max_occurs) > 1

        section.sub_sections.append(SubSection(
            section_def=attribute_section, nx_kind='attribute',
            name=f'nx_attribute_{name}', repeats=repeats))


def add_group_properties(xml_node: ET.Element, section: Section):
    '''
    Adds all properties that can be generated from the given nexus group XML node to
    the given (empty) metainfo section definition.
    '''
    for group in xml_node.findall('nx:group', XML_NAMESPACES):
        group_section = create_group_section(group, section)
        section.inner_section_definitions.append(group_section)
        if 'name' in group.attrib:
            name = f'nx_group_{group.attrib["name"]}'
        else:
            name = f'nx_group_{group.attrib["type"].replace("NX", "").upper()}'
        max_occurs = group.attrib.get('maxOccurs', '0')
        repeats = any(name_char.isupper()
                      for name_char in name) or max_occurs == 'unbounded' or int(max_occurs) > 1
        section.sub_sections.append(SubSection(
            section_def=group_section, nx_kind='group', name=name, repeats=repeats))
    for field in xml_node.findall('nx:field', XML_NAMESPACES):
        field_section = create_field_section(field, section)
        name = field.attrib["name"]
        max_occurs = field.attrib.get('maxOccurs', '0')
        repeats = any(name_char.isupper()
                      for name_char in name) or max_occurs == 'unbounded' or int(max_occurs) > 1

        section.sub_sections.append(SubSection(
            section_def=field_section, nx_kind='field',
            name=f'nx_field_{name}', repeats=repeats))

    add_attributes(xml_node, section)


def add_template_properties(xml_node: ET.Element, section: Section):
    '''
    Adds potential abilities of a group or field section to act as a TEMPLATE or
    nameType="any" definition.
    '''
    is_template = section.name.lower() != section.name or 'name' not in xml_node.attrib
    if is_template:
        section.quantities.append(Quantity(
            name='nx_name', type=str, default=xml_node.attrib.get('name'), description='''
                This is a nexus template property. This quantity holds the actual name used
                in the nexus data.'''))


def add_base_section(section: Section, container: Section, default_base_section: Section = None):
    '''
    Potentially adds a base section to the given section, if the given container has
    a base-section with a suitable base.
    '''
    base_section = container.all_inner_section_definitions.get(section.name, None)
    if base_section:
        assert base_section.nx_kind == section.nx_kind, 'base section has wrong nexus kind'
    else:
        base_section = default_base_section

    if base_section:
        section.base_sections = [base_section]


def create_attribute_section(xml_node: ET.Element, container: Section) -> Section:
    '''
    Creates a metainfo section from the nexus attribute given as xml node.
    '''
    xml_attrs = xml_node.attrib
    assert 'name' in xml_attrs, 'attribute has not name'

    attribute_section = Section(
        validate=VALIDATE, nx_kind='attribute',
        name=xml_attrs['name'] + 'Attribute')
    add_base_section(attribute_section, container)
    container.inner_section_definitions.append(attribute_section)

    base_value_quantity = attribute_section.all_quantities.get('nx_value')
    if base_value_quantity is None:
        value_quantity = Quantity(
            name='nx_value', description='The value for this nexus attribute')
    else:
        value_quantity = base_value_quantity.m_copy()
    attribute_section.quantities.append(value_quantity)

    enum_type = get_enum(xml_node)
    if enum_type is not None:
        value_quantity.type = enum_type

    if value_quantity.type is None:
        value_quantity.type = str

    add_common_properties(xml_node, attribute_section)
    add_template_properties(xml_node, attribute_section)

    return attribute_section


def create_field_section(xml_node: ET.Element, container: Section):
    '''
    Creates a metainfo section from the nexus field given as xml node.
    '''
    xml_attrs = xml_node.attrib

    assert 'name' in xml_attrs, 'field has not name'
    name = xml_attrs['name'] + 'Field'
    field_section = Section(validate=VALIDATE, nx_kind='field', name=name)
    add_base_section(field_section, container)
    container.inner_section_definitions.append(field_section)

    add_template_properties(xml_node, field_section)

    base_value_quantity = field_section.all_quantities.get('nx_value')
    if base_value_quantity:
        value_quantity = base_value_quantity.m_copy()
    else:
        value_quantity = Quantity(name='nx_value', description='The value for this nexus field')
    field_section.quantities.append(value_quantity)

    if 'type' in xml_attrs:
        nx_type = xml_attrs['type']
    else:
        nx_type = 'NX_CHAR'
    if nx_type not in _NX_TYPES:
        raise NotImplementedError(f'type {nx_type} is not supported for {name}')
    field_section.more['nx_type'] = nx_type

    if value_quantity.type is None or value_quantity.type is Any or nx_type != 'NX_CHAR':
        value_quantity.type = _NX_TYPES[nx_type]

    enum_type = get_enum(xml_node)
    if enum_type:
        value_quantity.type = enum_type

    if value_quantity.type is None:
        value_quantity.type = Any

    if 'units' in xml_attrs:
        field_section.more['nx_units'] = xml_attrs['units']
        if xml_attrs['units'] != 'NX_UNITLESS':
            # TO DO a default could be created from the nx_units value
            field_section.quantities.append(Quantity(
                name='nx_unit', type=Unit,
                description='The specific unit for that this fields data has.',
                a_elasticsearch=Elasticsearch()))

    dimensions = xml_node.find('nx:dimensions', XML_NAMESPACES)
    if dimensions is not None:
        shape = []
        for dimension in dimensions.findall('nx:dim', XML_NAMESPACES):
            dimension_value: Any = dimension.attrib.get('value', '*')
            try:
                dimension_value = int(dimension_value)
            except ValueError:
                pass
            shape.append(dimension_value)
        value_quantity.shape = shape

    add_common_properties(xml_node, field_section)
    add_attributes(xml_node, field_section)

    return field_section


def create_group_section(xml_node: ET.Element, container: Section) -> Section:
    '''
    Creates a metainfo section from the nexus group given as xml node.
    '''
    xml_attrs = xml_node.attrib
    typ = xml_attrs['type']

    if 'name' in xml_attrs:
        name = xml_attrs['name'] + 'Group'
    else:
        name = typ + 'Group'

    group_section = Section(validate=VALIDATE, nx_kind='group', name=name)
    add_base_section(group_section, container, get_or_create_section(typ))

    add_common_properties(xml_node, group_section)
    add_template_properties(xml_node, group_section)
    add_group_properties(xml_node, group_section)

    return group_section


def create_class_section(xml_node: ET.Element) -> Section:
    '''
    Creates a metainfo section from the top-level nexus definition given as xml node.
    '''
    xml_attrs = xml_node.attrib
    assert 'name' in xml_attrs
    assert 'category' in xml_attrs

    class_section = get_or_create_section(xml_attrs['name'],
                                          nx_kind=xml_attrs['type'],
                                          nx_category=xml_attrs['category'])

    if 'extends' in xml_attrs:
        base_section = get_or_create_section(xml_attrs['extends'])
        class_section.base_sections = [base_section]

    add_common_properties(xml_node, class_section)
    add_group_properties(xml_node, class_section)

    return class_section


def sort_nxdl_files(paths):
    '''sorting all definitions based on dependencies'''
    def compare_dependencies(nxdl1, nxdl2):
        if 'extends' in nxdl1.attrib and nxdl1.attrib['extends'] == nxdl2.attrib['name']:
            return True
        for group1 in nxdl1.iter("*"):
            if group1.tag[group1.tag.rindex("}") + 1:] == 'group' and \
                    group1.attrib['type'] == nxdl2.attrib['name']:
                break
        else:
            return False
        for group2 in nxdl2.iter("*"):
            if group2.tag[group2.tag.rindex("}") + 1:] == 'group' and \
                    group2.attrib['type'] == nxdl1.attrib['name']:
                return False
        return True

    list_of_nxdl = []
    for path in paths:
        for nxdl_file in sorted(os.listdir(path)):
            if not nxdl_file.endswith('.nxdl.xml'):
                continue
            xml_tree = ET.parse(os.path.join(path, nxdl_file))
            xml_node = xml_tree.getroot()
            xml_node.set('nxdl_base', path)
            assert xml_node.attrib.get('type') == 'group', 'definition is not a group'
            list_of_nxdl.append(xml_node)
    sorted_index = 0
    while sorted_index < len(list_of_nxdl):
        current_index = sorted_index + 1
        while current_index < len(list_of_nxdl):
            if compare_dependencies(list_of_nxdl[sorted_index], list_of_nxdl[current_index]):
                list_of_nxdl.append(list_of_nxdl[sorted_index])
                list_of_nxdl.__delitem__(sorted_index)
                break
            current_index = current_index + 1
        if current_index == len(list_of_nxdl):
            sorted_index = sorted_index + 1
    # print('\n'.join([nxdl.attrib['name'] for nxdl in list_of_nxdl]))
    return list_of_nxdl


def add_section_from_nxdl(xml_node):
    '''
    Creates a metainfo section from an nxdl file.
    '''
    try:
        global _XML_PARENT_MAP  # pylint: disable=global-statement
        _XML_PARENT_MAP = {
            child: parent for parent in xml_node.iter() for child in parent}

        # The section gets already implicitly added to CURRENT_PACKAGE by get_or_create_section
        create_class_section(xml_node)

    except NotImplementedError as err:
        print('Exception while mapping ' + xml_node.attrib["name"] + ':', err, file=sys.stderr)


def create_package_from_nxdl_directories(paths) -> Package:
    '''
    Creates a metainfo package from the given nexus directory. Will generate the respective
    metainfo definitions from all the nxdl files in that directory.
    '''
    global CURRENT_PACKAGE  # pylint: disable=global-statement
    CURRENT_PACKAGE = Package(name=f'nexus')

    sorted_files = sort_nxdl_files(paths)
    for nxdl_file in sorted_files:
        add_section_from_nxdl(nxdl_file)

    return CURRENT_PACKAGE


# separated metainfo package for the nexus base classes, application defs and contributed classes.
DIRS = [os.path.join(nexus.get_nexus_definitions_path(), 'base_classes')]
DIRS.append(os.path.join(nexus.get_nexus_definitions_path(), 'contributed_definitions'))
DIRS.append(os.path.join(nexus.get_nexus_definitions_path(), 'applications'))
APPLICATIONS = create_package_from_nxdl_directories(DIRS)
PACKAGES = (APPLICATIONS,)  # , APPLICATIONS, CONTRIBUTED)

# We take the application definitions and create a common parent section that allows to
# include nexus in an EntryArchive.
NEXUS_SECTION = Section(validate=VALIDATE, name='Nexus')

for application_section in APPLICATIONS.section_definitions:  # pylint: disable=not-an-iterable
    if application_section.more.get('nx_category') == 'application':
        sub_section = SubSection(
            section_def=application_section,
            name=application_section.name.replace('NX', 'nx_application_'))
        NEXUS_SECTION.sub_sections.append(sub_section)

APPLICATIONS.section_definitions.append(NEXUS_SECTION)

ENTRY_ARCHIVE_NEXUS_SUB_SECTION = \
    SubSection(name='nexus',
               section_def=NEXUS_SECTION)
# SubSection(name='nexus',
#            section_def=NEXUS_SECTION,
#            a_elasticsearch=Elasticsearch(nested=True))
EntryArchive.nexus = ENTRY_ARCHIVE_NEXUS_SUB_SECTION  # type: ignore
EntryArchive.m_def.sub_sections.append(ENTRY_ARCHIVE_NEXUS_SUB_SECTION)


# We need to initialize the metainfo definitions. This is usually done automatically,
# when the metainfo schema is defined though MSection Python classes.
for package in PACKAGES:
    package.init_metainfo()


# We skip the Python code generation for now and offer Python classes as variables
# TO DO not necessary right now, could also be done case-by-case by the nexus parser
PYTHON_MODULE = sys.modules[__name__]
for package in PACKAGES:
    for sektion in package.section_definitions:  # pylint: disable=not-an-iterable
        setattr(PYTHON_MODULE, sektion.name, sektion.section_cls)
