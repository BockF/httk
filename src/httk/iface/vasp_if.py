#
#    The high-throughput toolkit (httk)
#    Copyright (C) 2012-2015 Rickard Armiento
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os, shutil, math, re, bz2, subprocess

import httk

from datetime import datetime
from httk import config
from httk.core.template import apply_templates
from httk.atomistic.data import periodictable
from httk.core.ioadapters import cleveropen
from httk.core import *
from httk.core.basic import mkdir_p, micro_pyawk
from httk.atomistic import Structure
from httk.atomistic.structureutils import cartesian_to_reduced
from httk.task.reader import read_manifest
from httk.core.crypto import hexhash_str


def get_pseudopotential(species, poscarspath=None):
    if poscarspath is None:
        try:
            poscarspath = config.get('paths', 'vasp_pseudolib')
        except Exception:
            #return [name for name in os.listdir(a_dir)
            #        if os.path.isdir(os.path.join(a_dir, name))]
            poscarspath = None

    if poscarspath is None and "VASP_PSEUDOLIB" in os.environ:
        poscarspath = os.environ['VASP_PSEUDOLIB']

    if poscarspath is None:
        raise Exception("httk.iface.vasp_if.get_pseudopotentials: No path given for where to find VASP pseudopotentials. \
                         Please either set vasp_pseudolib in httk.cfg, or define the VASP_PSEUDOLIB variable, or \
                         pass along a string in your code for the parameter 'poscarspath'")

    poscarspath = os.path.expanduser(poscarspath)

    for priority in ["_3", "_2", "_d", "_pv", "_sv", "", "_h", "_s"]:
        basepath = os.path.join(poscarspath, species)
        if os.path.exists(basepath+priority):
            try:
                f = cleveropen(os.path.join(basepath+priority, 'POTCAR'), 'r')
                data = f.read()
                f.close()
                return data
            except Exception:
                raise
                pass

    raise Exception("httk.iface.vasp_if.get_pseudopotentials: could not find a suitable pseudopotential for "+str(species))


def write_kpoints_file(fio, kpoints, comment=None, mp=True, gamma_centered=False):
    """
    """
    fio = IoAdapterFileWriter.use(fio)
    f = fio.file
    f.write(str(comment)+"\n")
    f.write("0\n")
    #if mp:
    #    f.write("Monkhorst-Pack\n")
    #else:
    #    f.write("Gamma\n")
    if gamma_centered:
        f.write("Gamma\n")
    else:
        f.write("Monkhorst-Pack\n")
    f.write(" ".join([str(x) for x in kpoints])+"\n")
    fio.close()


def write_generic_kpoints_file(fio, comment=None, mp=True):
    """
    """
    fio = IoAdapterFileWriter.use(fio)
    f = fio.file
    f.write(str(comment)+"\n")
    f.write("0\n")
    #if mp:
    #    f.write("Monkhorst-Pack\n")
    #else:
    #    f.write("Gamma\n")
    f.write("Auto\n")
    f.write("20\n")
    fio.close()


def get_magmom(symbol):
    return 8

#magions = ['Sc','Ti','V','Cr','Mn','Fe','Co','Ni','Cu','Zn','Y','Zr','Nb','Mo','Tc','Ru','Rh','Pd','Ag','Cd','La','Hf','Ta','W','Re','Os','Ir','Pt','Au','Hg','Ce','Pr','Nd','Pm','Sm','Eu','Gd','Tb','Dy','Ho','Er','Tm','Yb','Lu','Th','Pa','U']
# From: A. Jain et al. / Computational Materials Science 50 (2011) 2295-2310
magions = ['Ag', 'Au', 'Cd', 'Ce', 'Co', 'Cr', 'Cu', 'Dy', 'Er', 'Eu', 'Fe', 'Gd', 'Hf', 'Hg', 'Ho', 'Ir', 'La', 'Lu', 'Mn', 'Mo', 'Nb', 'Nd', 'Ni', 'Os', 'Pa', 'Pd', 'Pm', 'Pr', 'Pt', 'Re', 'Rh', 'Ru', 'Sc', 'Sm', 'Ta', 'Tb', 'Tc', 'Th', 'Ti', 'Tm', 'U', 'V', 'W', 'Y', 'Yb', 'Zn', 'Zr']
dualmag = {'O': ['Co'], 'S': ['Mn', 'Fe', 'Cr', 'Co']}


def is_dualmagnetic(ion, ionlist):
    for i in range(len(ionlist)):
        if ionlist[i] in dualmag:
            if ion in dualmag[ionlist[i]]:
                return True
    return False


def magnetization_recurse(basemags, dualmags, high, low):
    if len(dualmags) == 0:
        return [basemags]

    index = dualmags.pop()
    basemags[index] = high
    hi_list = magnetization_recurse(list(basemags), list(dualmags), high, low)
    basemags[index] = low
    low_list = magnetization_recurse(list(basemags), list(dualmags), high, low)

    return hi_list + low_list


def get_magnetizations(ionlist, high, low):
    basemags = []
    dualmags = []
    for i in range(len(ionlist)):
        if is_dualmagnetic(ionlist[i], ionlist):
            basemags.append(None)
            dualmags.append(i)
        else:
            if ionlist[i] in magions:
                basemags.append(high)
            else:
                basemags.append(low)

    return magnetization_recurse(basemags, dualmags, high, low)


def copy_template(dirtemplate, dirname, templatename):
    template = os.path.join(dirname, "ht.template."+templatename)
    if os.path.exists(template):
        raise Exception("Template dir already exists.")
    shutil.copytree(dirtemplate, template, True)


def poscar_to_strs(fio, included_decimals=''):
    """
    Parses a file on VASPs POSCAR format. Returns
      (cell, scale, vol, coords, coords_reduced, counts, occupations, comment)
    where
      cell: 3x3 nested list of *strings* designating the cell
      scale: *string* representing the overall scale of the cell
      vol: *string* representing the volume of the cell (only one of scale and vol will be set, the other one = None)
      coords: Nx3 nested list of *strings* designating the coordinates
      coords_reduced: bool, true = coords are given in reduced coordinate (in vasp D or Direct), false = coords are given in cartesian coordinates
      counts: how many atoms of each type
      occupations: which species of each atom type (integers), or -1, ... -N if no species are given.
      comment: the comment string given at the top of the file
    """
    fio = IoAdapterFileReader.use(fio)
    f = fio.file

    fi = iter(f)

    comment = next(fi).strip()
    vol_or_scale = next(fi).strip()
    vol_or_scale_nbr = float(vol_or_scale)
    if vol_or_scale_nbr < 0:
        vol = vol_or_scale[1:]
        scale = None
    else:
        scale = vol_or_scale
        vol = None

    cell = [['', '', ''], ['', '', ''], ['', '', '']]
    for i in [0, 1, 2]:
        cellline = next(fi).strip().split()
        for j, v in enumerate(cellline):
            cell[i][j] = v

    symbols_or_count = next(fi).strip().split()

    try:
        counts = list(map(int, symbols_or_count))
        symbols = None
        occupations = range(-1, -len(counts)-1, -1)
    except Exception:
        symbols = symbols_or_count
        counts = [int(s) for s in next(fi).strip().split()]
        occupations = [periodictable.numbers[symbol] for symbol in symbols]

    N = sum(counts)

    coordtype_or_selectivedynamics = next(fi).strip()
    if coordtype_or_selectivedynamics[0] in 'Ss':
        # Skip row if selective dynamics specifier
        coordtype = next(fi).strip()
    else:
        coordtype = coordtype_or_selectivedynamics

    if coordtype[0] in 'CcKk':
        coords_reduced = True
    else:
        coords_reduced = False

    coords = []
    if included_decimals == '':
        for i in range(N):
            nxt = next(fi)
            strcoord = nxt.strip().split()[:3]
            coord = list(map(lambda x: x.strip(), strcoord))
            coords.append(coord)
    else:
        for i in range(N):
            nxt = next(fi)
            strcoord = nxt.strip().split()[:3]
            tempcoord = list(map(lambda x: x.strip(), strcoord))
            coord = list(map(lambda x: x[0:2+included_decimals], tempcoord))
            coords.append(coord)

    return (cell, scale, vol, coords, coords_reduced, counts, occupations, comment)


def poscar_to_structure(f, included_decimals='', structure_class=Structure):
    cell, scale, volume, coords, coords_reduced, counts, occupations, comment = poscar_to_strs(f, included_decimals)

    frac_cell = FracVector.create(cell, simplify=True)
    counts = [int(x) for x in counts]

    if coords_reduced:
        frac_coords = cartesian_to_reduced(cell, coords)
    else:
        frac_coords = FracVector.create(coords, simplify=True)

    if volume is not None:
        volume = FracScalar.create(volume)

    if scale is not None:
        scale = FracScalar.create(scale)

    newoccupations = []
    for occupation in occupations:
        newoccupations.append(periodictable.atomic_number(occupation))

    struct = structure_class.create(uc_basis=frac_cell, uc_volume=volume, uc_scale=scale, uc_reduced_coords=frac_coords, uc_counts=counts, assignments=newoccupations, tags={'comment': comment}, periodicity=0)

    return struct


def write_poscar(fio, cell, coords, coords_reduced, counts, occupations, comment="Comment", scale="1", vol=None):
    """
    Writes a file on VASPs POSCAR format. Where it says *string* below, any type that works with str(x) is also ok.

    Input arguments
      f: file stream to put output on
      cell: 3x3 nested list of *strings* designating the cell
      coords: Nx3 nested list of *strings* designating the coordinates
      coords_reduced: bool, true = coords are given in reduced coordinate (in vasp D or Direct), false = coords are given in cartesian coordinates
      counts: how many atoms of each type
      occupations: which species of each atom type
      comment: (optional) the comment string given at the top of the file
      scale: (optional) *string* representing the overall scale of the cell
      vol: *string* representing the volume of the cell (only one of scale and vol can be set)
    """
    fio = IoAdapterFileWriter.use(fio)
    f = fio.file
    f.write(str(comment)+"\n")
    if vol is not None:
        f.write("-"+str(vol)+"\n")
    else:
        f.write(str(scale)+"\n")
    for c1, c2, c3 in cell:
        f.write(str(c1)+" "+str(c2)+" "+str(c3)+"\n")

    for i in range(len(counts)):
        if occupations is None:
            f.write(periodictable.symbols[i] + " ")
        else:
            f.write(str(occupations[i]) + " ")
    f.write("\n")

    for count in counts:
        f.write(str(count) + " ")
    f.write("\n")
    if coords_reduced:
        f.write("D\n")
    else:
        f.write("K\n")
    for c1, c2, c3 in coords:
        f.write(str(c1)+" "+str(c2)+" "+str(c3)+"\n")


def structure_to_comment(struct):
    tags = struct.get_tags().values()
    if len(tags) > 0:
        tagstr = " tags: " + ", ".join([tag.tag+":"+tag.value for tag in tags])
    else:
        tagstr = ""
    if struct.has_rc_repr and struct.has_uc_repr:
        return struct.formula + " " + struct.hexhash + tagstr
    else:
        return struct.formula + " " + tagstr


def structure_to_poscar(f, struct, fix_negative_determinant=False, comment=None, primitive_cell=True):
    if comment is None:
        comment = structure_to_comment(struct)
    if primitive_cell:
        basis = struct.pc.uc_basis
        coords = struct.pc.uc_reduced_coords
        vol = struct.pc.uc_volume
        counts = struct.pc.uc_counts
    else:
        #basis = struct.cc.uc_basis
        #coords = struct.cc.uc_reduced_coords
        #vol = struct.cc.uc_volume
        #counts = struct.cc.uc_counts
        basis = struct.uc_basis
        coords = struct.uc_reduced_coords
        vol = struct.uc_volume
        counts = struct.uc_counts

    if basis.det() < 0:
        if fix_negative_determinant:
            basis = -basis
            coords = (-coords).normalize()

    write_poscar(f, basis.to_strings(), coords.to_strings(), True, counts, struct.symbols, comment, vol=vol.to_string())


def calculate_kpoints(struct, dens=20):
    #local KPTSLINE=$(awk -v "LVAL=$LVAL" -v"equalkpts=$EQUAL_KPTS" -v"bumpkpts=$BUMP_KPTS" '
    basis = struct.uc_basis

    celldet = basis.det()
    cellvol = abs(celldet)

    if cellvol == 0:
        raise Exception("vasp_if.calculate_kpoints: Error in VASP_KPOINTSLINE: singular cell vectors. POSCAR is broken.")

    recip = basis.reciprocal().simplify()
    half = 0.5
    N1 = int(math.ceil(math.sqrt(recip[0].lengthsqr())*dens+half)+0.1)
    N2 = int(math.ceil(math.sqrt(recip[1].lengthsqr())*dens+half)+0.1)
    N3 = int(math.ceil(math.sqrt(recip[2].lengthsqr())*dens+half)+0.1)
    return max(1, N1), max(1, N2), max(1, N3)


def prepare_single_run(dirpath, struct, poscarspath=None, template='t:/vasp/single/static', overwrite=False):
    if overwrite:
        mkdir_p(dirpath)
    else:
        os.mkdir(dirpath)
    structure_to_poscar(os.path.join(dirpath, "POSCAR"), struct, fix_negative_determinant=True)
    #write_generic_kpoints_file(os.path.join(dirpath,"KPOINTS"),comment=structure_to_comment(struct))
    kpoints = calculate_kpoints(struct)
    write_kpoints_file(os.path.join(dirpath, "KPOINTS"), kpoints, comment=structure_to_comment(struct))
    ioa = IoAdapterFileWriter.use(os.path.join(dirpath, "POTCAR"))
    f = ioa.file
    spieces_counts = []
    magmomlist = get_magnetizations(struct.symbols, 5, 1)
    magmom_per_ion = magmomlist[0]
    magmoms = []
    nelect = 0
    natoms = 0
    nmag = 0
    for i in range(len(struct.assignments)):
        assignment = struct.assignments[i]
        count = struct.uc_counts[i]
        symbol = periodictable.atomic_symbol(assignment.symbol)
        pp = get_pseudopotential(symbol, poscarspath)
        f.write(pp)
        spieces_counts.append(count)
        #magmoms.append(str(count)+"*"+str(get_magmom(symbol)))
        magmom = magmom_per_ion[i]
        magmoms.append(str(count)+"*"+str(magmom))

        def zval(results, match):
            results['zval'] = float(match.group(1))
        results = micro_pyawk(IoAdapterString(pp), [["^ *POMASS.*; *ZVAL *= *([^ ]+)", None, zval]])
        if not 'zval' in results:
            raise Exception("vasp_if.prepare_simple_static_run: Could not read ZVAL from potcar file")
        nelect += results['zval']*count
        natoms += count
        nmag += count*magmom

    ioa.close()
    nbands1 = int(0.6*nelect + 1.0)+int(math.ceil(natoms/2.0)+0.1)
    nbands2 = int(0.6*nelect + 1.0)+int(math.ceil(nmag/2.0)+0.1)
    nbands3 = int(0.6*nelect + 1.0)+20
    nbands_spin = max(1, nbands1, nbands2, nbands3)
    nbands_spin += nbands_spin % 2
    nbands1 = int(nelect/2.0 + 2)+int(math.ceil(natoms/2.0)+0.1)
    nbands2 = int(math.ceil(nelect/2.0)+20+0.1)
    nbands_nospin = max(1, nbands1, nbands2)
    nbands_nospin += nbands_spin % 2

    data = {}
    data['VASP_SPIECES_COUNTS'] = " ".join(list(map(str, spieces_counts)))
    data['VASP_MAGMOM'] = " ".join(list(map(str, magmoms)))
    data['VASP_NBANDS_SPIN'] = str(nbands_spin)
    data['VASP_NBANDS_NOSPIN'] = str(nbands_nospin)

    if template.startswith('t:'):
        template = os.path.join(httk.httk_root, 'Execution', 'tasks-templates', template[2:])

    apply_templates(template, dirpath, envglobals=data, mkdir=False)


class OutcarReader():

    def __init__(self, ioa):
        self.ioa = ioa
        self.parse()
        pass

    def parse(self):
        results = {'final': False}

        def set_final(results, match):
            results['final'] = True

        def read_energy(results, match):
            self.final_energy_with_entropy = match.group(1)
            self.final_energy = match.group(2)
        results = micro_pyawk(self.ioa, [
                              ["^ *energy *without *entropy= *([^ ]+) *energy\(sigma->0\) *= *([^ ]+) *$", None, read_energy],
                              ["FREE ENERGIE", None, set_final],
                              ], results, debug=False)
        self.parsed = True


def read_outcar(ioa):
    return OutcarReader(ioa)


def clean_and_compress(path, remove = []):

    httk_path = '/'.join(httk.__path__[0].split('/')[:-2])
    vasptools = os.path.join(httk_path, 'Execution/tasks/vasp/vasptools.sh')
    task_api = os.path.join(httk_path, 'Execution/tasks/ht_tasks_api.sh')

    bashscript = ['#!/bin/bash',
        'source ' +  vasptools + '; VASP_CLEAN_OUTCAR',
        'source ' + task_api + '; HT_TASK_COMPRESS']

    home = os.getcwd()
    os.chdir(os.path.join(home, path))
    current_dir = os.getcwd()

    for filename in remove:
        if os.path.exists(os.path.join(current_dir, filename)):
            os.system(f'rm {filename}')

    if os.path.exists(os.path.join(current_dir, 'OUTCAR')):

        with open('clean.sh', 'w') as f:
            f.write('\n'.join(bashscript))


        os.system('chmod +x clean.sh')
        subprocess.run(os.path.join(current_dir,'clean.sh'))

        try:
            os.system('rm clean.sh.bz2')
            os.system('rm OUTCAR.bz2')
        except:
            pass

    os.chdir(home)
    return 0


def generate_computation(path):

    '''Generates httk Computation object from non-httk VASP calculation.
    Needs further generalization to include QE, FHI Aims, etc....'''

    for filename in os.listdir(path):
        if re.search('OUTCAR', os.path.join(path, filename)) is not None:
            info = parse_outcar(os.path.join(path, filename))

    (manifest_hash, signatures, project_key, keys) = read_manifest(os.path.join(path, 'ht.manifest.bz2'))

    computation = Computation.create(computation_date = ' '.join([info['Date'], info['Time']]),
            description = None,
            code = Code(info['Code'], info['Version']),
            manifest_hash = manifest_hash,
            signatures = signatures,
            keys = keys,
            relpath = path,
            project_counter = 0,
            added_date = datetime.now())


    for key in info.keys():
        computation.add_tag(key, str(info[key]))

    return computation


def parse_outcar(path):

    '''Parse the OUTCAR file for all important information about the
    non-httk calculation. Returns filled dictionary with information
    required for reproduction of results.'''

    calc_info = {'Code': None,
            'Version': None,
            'Build': None,
            'ENCUT': None,
            'NKPTS': None,
            'XC': None,
            'Potentials': [],
            'Date': None,
            'Time': None,
            'Elapsed': None,
            'System': None,
            'Nodes': None,
            'Cores': None}

    system_cpn = {'Lumi': 128,
            'Dardel': 128,
            'Tetralith': 32,
            'Sigma': 32}

    with bz2.open(path, 'rt') as f:
        for text in f:
            line = text.rstrip('\n')
            if not None in calc_info.values():
                break
            if calc_info['Code'] is None:
                if re.search('vasp\.\d\.\d\.\d\S+', line) is not None:
                    data = line.split()[0].split('.')
                    if data[0] == 'vasp':
                        calc_info['Code'] = 'VASP'
                        calc_info['Version'] =  '.'.join(data[1:4])
                        calc_info['Build'] = data[-1]
            if re.search('POTCAR\S+', line) is not None:
                pot = ' '.join(line.split()[-2:])
                if not pot in calc_info['Potentials']:
                    calc_info['Potentials'].append(pot)
            if calc_info['System'] is None:
                if re.search('executed on', line) is not None:
                    data = line.split()
                    if 'LUMI' in data[2]:
                        calc_info['System'] = 'Lumi'
                    elif 'TETRALITH' in data[2]:
                        calc_info['System'] = 'Tetralith'
                    else:
                        calc_info['System'] = data[2]
                    calc_info['Date'] = data[4]
                    calc_info['Time'] = data[-1]
            if calc_info['XC'] is None:
                if re.search('LEXCH', line) is not None:
                    calc_info['XC'] = line.split()[-1]
            if calc_info['Cores'] is None:
                if re.search('running on', line) is not None:
                    calc_info['Cores'] = int(line.split()[2])
            if calc_info['ENCUT'] is None:
                if re.search('ENCUT', line) is not None:
                    calc_info['ENCUT'] = float(line.split()[2])
            if calc_info['NKPTS'] is None:
                if re.search('NKPTS', line) is not None:
                    calc_info['NKPTS'] = int(line.split()[3])
            if calc_info['Elapsed'] is None:
                if re.search('Elapsed time', line) is not None:
                    calc_info['Elapsed'] = float(line.split()[-1])
            if not calc_info['Cores'] is None and not calc_info['System'] is None:
                calc_info['Nodes'] = int(calc_info['Cores']/system_cpn[calc_info['System']])

    return calc_info


def generate_result(path):

    '''Converts calculation in the defined path into simple
    httk relaxed cell result object.'''
    from httk.atomistic.results import Result_TotalEnergyResult
    struct = None

    for filename in os.listdir(path):
        if re.search('OUTCAR', filename) is not None:
            outcar = httk.iface.vasp_if.read_outcar(os.path.join(path, filename))
            final_energy = float(outcar.final_energy)
        if re.search('CONTCAR', filename) is not None:
            struct = httk.iface.vasp_if.poscar_to_structure(os.path.join(path, filename), included_decimals = 5)

    if struct == None:
        # If no CONTCAR file is found, try to load POSCAR instead.
        # If this fails again, no result object is returned.
        try:
            httk.iface.vasp_if.poscar_to_structure(os.path.join(path, 'POSCAR'), included_decimals = 5)
        except Exception as e:
            print(e)
            return None

    computation = generate_computation(path)

    return Result_TotalEnergyResult(
            computation = computation,
            structure = struct,
            total_energy = final_energy)


def httkify(path, remove = [], keypath = None):

    '''Takes non-httk VASP calculation and turns it into httk-like object, cleaning and compressing
    the running directory. Returns default TotalEnergyResult. To fully work, the {path} need to lead to
    a directory which contains a valid VASP OUTCAR, but files such as KPOINTS, INCAR, etc. may be kept
    for sake of traceability. The {remove} variable defines a list of files to delete from the target
    directory.'''

    if keypath is None:

        # If no keypath specified, try standard path 'ht.project/keys'
        try:
            sk, pk = httk.core.crypto.read_keys('ht.project/keys')
        except:
            print(f'Please specify keypath for conversion to httk-like object, or consider turning this directory into a httk project.')
            exit()

    else:

        # Workflow to get clean and compress calculation, and create manifest and httk-like object
        clean_and_compress(path, remove = remove)
        with bz2.open(os.path.join(path, 'ht.manifest.bz2'), 'wt') as manifest_file:

            sk, pk = httk.core.crypto.read_keys(keypath)
            print(f'Generating manifest for {path}, this may take some time!')
            httk.core.crypto.manifest_dir(
                basedir = path,
                manifestfile = manifest_file,
                excludespath = '',
                keydir = keypath,
                sk = sk,
                pk = pk,
                force = True)

        return generate_result(path)


