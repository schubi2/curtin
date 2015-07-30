#   Copyright (C) 2013 Canonical Ltd.
#
#   Author: Scott Moser <scott.moser@canonical.com>
#
#   Curtin is free software: you can redistribute it and/or modify it under
#   the terms of the GNU Affero General Public License as published by the
#   Free Software Foundation, either version 3 of the License, or (at your
#   option) any later version.
#
#   Curtin is distributed in the hope that it will be useful, but WITHOUT ANY
#   WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
#   FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for
#   more details.
#
#   You should have received a copy of the GNU Affero General Public License
#   along with Curtin.  If not, see <http://www.gnu.org/licenses/>.

from collections import OrderedDict
from curtin import block
from curtin import util
from curtin.log import LOG

from . import populate_one_subcmd

import glob
import os
import platform
import string
import sys
import tempfile
import time

# Sometimes when a seed is being generated to build an installer image parted
# might not be available, so don't import it unless it is, and only fail if it
# isn't available when it is actually being used
try:
    import parted
except ImportError:
    pass

SIMPLE = 'simple'
SIMPLE_BOOT = 'simple-boot'
CUSTOM = 'custom'

CUSTOM_REQUIRED_PACKAGES = ['mdadm', 'lvm2', 'bcache-tools']

CMD_ARGUMENTS = (
    ((('-D', '--devices'),
      {'help': 'which devices to operate on', 'action': 'append',
       'metavar': 'DEVICE', 'default': None, }),
     ('--fstype', {'help': 'root partition filesystem type',
                   'choices': ['ext4', 'ext3'], 'default': 'ext4'}),
     (('-t', '--target'),
      {'help': 'chroot to target. default is env[TARGET_MOUNT_POINT]',
       'action': 'store', 'metavar': 'TARGET',
       'default': os.environ.get('TARGET_MOUNT_POINT')}),
     ('--boot-fstype', {'help': 'boot partition filesystem type',
                        'choices': ['ext4', 'ext3'], 'default': None}),
     ('mode', {'help': 'meta-mode to use',
               'choices': [CUSTOM, SIMPLE, SIMPLE_BOOT]}),
     )
)


def block_meta(args):
    # main entry point for the block-meta command.
    state = util.load_command_environment()
    cfg = util.load_command_config(args, state)
    if args.mode == CUSTOM or cfg.get("storage") is not None:
        meta_custom(args)
    elif args.mode in (SIMPLE, SIMPLE_BOOT):
        meta_simple(args)
    else:
        raise NotImplementedError("mode=%s is not implemented" % args.mode)


def logtime(msg, func, *args, **kwargs):
    with util.LogTimer(LOG.debug, msg):
        return func(*args, **kwargs)


def write_image_to_disk(source, dev):
    """
    Write disk image to block device
    """
    (devname, devnode) = block.get_dev_name_entry(dev)
    util.subp(args=['sh', '-c',
                    ('wget "$1" --progress=dot:mega -O - |'
                     'tar -SxOzf - | dd of="$2"'),
                    '--', source, devnode])
    util.subp(['partprobe', devnode])
    util.subp(['udevadm', 'settle'])
    return block.get_root_device([devname, ])


def get_bootpt_cfg(cfg, enabled=False, fstype=None, root_fstype=None):
    # 'cfg' looks like:
    #   enabled: boolean
    #   fstype: filesystem type (default to 'fstype')
    #   label:  filesystem label (default to 'boot')
    # parm enable can enable, but not disable
    # parm fstype overrides cfg['fstype']
    def_boot = platform.machine() in ('aarch64')
    ret = {'enabled': def_boot, 'fstype': None, 'label': 'boot'}
    ret.update(cfg)
    if enabled:
        ret['enabled'] = True

    if ret['enabled'] and not ret['fstype']:
        if root_fstype:
            ret['fstype'] = root_fstype
        if fstype:
            ret['fstype'] = fstype
    return ret


def get_partition_format_type(cfg, machine=None, uefi_bootable=None):
    if machine is None:
        machine = platform.machine()
    if uefi_bootable is None:
        uefi_bootable = util.is_uefi_bootable()

    cfgval = cfg.get('format', None)
    if cfgval:
        return cfgval

    if uefi_bootable:
        return 'uefi'

    if machine in ['aarch64']:
        return 'gpt'
    elif machine.startswith('ppc64'):
        return 'prep'

    return "mbr"


def wipe_volume(path, wipe_type):
    cmds = []
    if wipe_type == "pvremove":
        # We need to use --force --force in case it's already in a volgroup and
        # pvremove doesn't want to remove it
        cmds.append(["pvremove", "--force", "--force", "--yes", path])
        cmds.append(["pvscan", "--cache"])
        cmds.append(["vgscan", "--mknodes", "--cache"])
    elif wipe_type == "zero":
        cmds.append(["dd", "bs=512", "if=/dev/zero", "of=%s" % path])
    elif wipe_type == "random":
        cmds.append(["dd", "bs=512", "if=/dev/urandom", "of=%s" % path])
    elif wipe_type == "superblock":
        cmds.append(["sgdisk", "--zap-all", path])
        cmds.append(["wipefs", "-a", path])
    else:
        raise ValueError("wipe mode %s not supported" % wipe_type)
    # Dd commands will likely exit with 1 when they run out of space. This
    # is expected and not an issue. If pvremove is run and there is no label on
    # the system, then it exits with 5. That is also okay, because we might be
    # wiping something that is already blank
    for cmd in cmds:
        util.subp(cmd, rcs=[0, 1, 2, 5], capture=True)


def devsync(devpath):
    util.subp(['partprobe', devpath])
    util.subp(['udevadm', 'settle'])
    for x in range(0, 10):
        if os.path.exists(devpath):
            return
        else:
            LOG.debug('Waiting on device path: {}'.format(devpath))
            time.sleep(1)
    raise OSError('Failed to find device at path: {}'.format(devpath))


def get_path_to_storage_volume(volume, storage_config):
    # Get path to block device for volume. Volume param should refer to id of
    # volume in storage config

    devsync_vol = None
    vol = storage_config.get(volume)
    if not vol:
        raise ValueError("volume with id '%s' not found" % volume)

    # Find path to block device
    if vol.get('type') == "partition":
        # For partitions, parted.Disk of parent disk, then find what number
        # partition it is and use parted.Partition.path
        partnumber = vol.get('number')
        if not partnumber:
            partnumber = 1
            for key, item in storage_config.items():
                if item.get("type") == "partition" and \
                        item.get("device") == vol.get("device"):
                    if item.get("id") == vol.get("id"):
                        break
                    else:
                        partnumber += 1
        disk_block_path = get_path_to_storage_volume(vol.get('device'),
                                                     storage_config)
        pdev = parted.getDevice(disk_block_path)
        pdisk = parted.newDisk(pdev)
        ppartitions = pdisk.partitions
        try:
            volume_path = ppartitions[partnumber - 1].path
        except IndexError:
            raise ValueError("partition '%s' does not exist" % vol.get('id'))
        devsync_vol = disk_block_path

    elif vol.get('type') == "disk":
        # Get path to block device for disk. Device_id param should refer
        # to id of device in storage config
        if vol.get('serial'):
            volume_path = block.lookup_disk(vol.get('serial'))
        elif vol.get('path'):
            volume_path = vol.get('path')
        else:
            raise ValueError("serial number or path to block dev must be \
                specified to identify disk")

    elif vol.get('type') == "lvm_partition":
        # For lvm partitions, a directory in /dev/ should be present with the
        # name of the volgroup the partition belongs to. We can simply append
        # the id of the lvm partition to the path of that directory
        volgroup = storage_config.get(vol.get('volgroup'))
        if not volgroup:
            raise ValueError("lvm volume group '%s' could not be found"
                             % vol.get('volgroup'))
        volume_path = os.path.join("/dev/", volgroup.get('name'),
                                   vol.get('name'))

    elif vol.get('type') == "dm_crypt":
        # For dm_crypted partitions, unencrypted block device is at
        # /dev/mapper/<dm_name>
        dm_name = vol.get('dm_name')
        if not dm_name:
            dm_name = vol.get('id')
        volume_path = os.path.join("/dev", "mapper", dm_name)

    elif vol.get('type') == "raid":
        # For raid partitions, block device is at /dev/mdX
        name = vol.get('id')
        volume_path = os.path.join("/dev", name)

    elif vol.get('type') == "bcache":
        # For bcache setups, the only reliable way to determine the name of the
        # block device is to look in all /sys/block/bcacheX/ dirs and see what
        # block devs are in the slaves dir there. Then, those blockdevs can be
        # checked against the kname of the devs in the config for the desired
        # bcache device. This is not very elegant though
        backing_device_kname = os.path.split(get_path_to_storage_volume(
            vol.get('backing_device'), storage_config))[-1]
        sys_path = list(filter(lambda x: backing_device_kname in x,
                               glob.glob("/sys/block/bcache*/slaves/*")))[0]
        while "bcache" not in os.path.split(sys_path)[-1]:
            sys_path = os.path.split(sys_path)[0]
        volume_path = os.path.join("/dev", os.path.split(sys_path)[-1])

    else:
        raise NotImplementedError("cannot determine the path to storage \
            volume '%s' with type '%s'" % (volume, vol.get('type')))

    # sync devices
    if not devsync_vol:
        devsync_vol = volume_path
    devsync(devsync_vol)

    return volume_path


def disk_handler(info, storage_config):
    ptable = info.get('ptable')

    disk = get_path_to_storage_volume(info.get('id'), storage_config)

    # Handle preserve flag
    if info.get('preserve'):
        if not ptable:
            # Don't need to check state, return
            return

        # Check state of current ptable
        try:
            (out, _err) = util.subp(["blkid", "-o", "export", disk],
                                    capture=True)
        except util.ProcessExecutionError:
            raise ValueError("disk '%s' has no readable partition table or \
                cannot be accessed, but preserve is set to true, so cannot \
                continue")
        current_ptable = list(filter(lambda x: "PTTYPE" in x,
                                     out.splitlines()))[0].split("=")[-1]
        if current_ptable == "dos" and ptable != "msdos" or \
                current_ptable == "gpt" and ptable != "gpt":
            raise ValueError("disk '%s' does not have correct \
                partition table, but preserve is set to true, so not \
                creating table, so not creating table." % info.get('id'))
        LOG.info("disk '%s' marked to be preserved, so keeping partition \
                 table")
        return

    # Wipe the disk
    if info.get('wipe') and info.get('wipe') != "none":
        try:
            # Shut down any volgroups, destroy all physical volumes
            volgroups = []
            pdev = parted.getDevice(disk)
            pdisk = parted.newDisk(pdev)
            partitions = list(partition.path for partition in pdisk.partitions)
            # We don't want to keep pdisk around if we're creating a new
            # partition table
            del(pdisk)
            (out, _err) = util.subp(["pvdisplay", "-C", "--separator", "=",
                                     "-o", "vg_name,pv_name", "--noheadings"],
                                    capture=True)
            for line in out.splitlines():
                if line.split('=')[-1] in partitions and \
                        line.split('=')[0] not in volgroups:
                    volgroups.append(line.split('=')[0].lstrip())
            if len(volgroups) > 0:
                util.subp(["vgremove", "--force"] + volgroups, capture=True)
            for partition in partitions:
                wipe_volume(partition, "pvremove")
            # Shut down bcache if any of the partitions contain bcache
            # superblocks
            uuids = []
            for partition in partitions:
                try:
                    (out, _err) = util.subp(["bcache-super-show", partition],
                                            capture=True)
                except util.ProcessExecutionError:
                    # bcache-super-show will fail if there is no valid bcache
                    # superblock
                    pass
                else:
                    # We found a valid bcache superblock on this disk,
                    # unregister its bcache device
                    for line in out.splitlines():
                        if "cset.uuid" in line:
                            uuid = line.rsplit()[-1]
                            if uuid not in uuids:
                                uuids.append(uuid)
            if len(uuids) > 0:
                for uuid in uuids:
                    with open(os.path.join("/sys/fs/bcache", uuid, "stop"),
                              "w") as fp:
                        fp.write("1")
                util.subp(["modprobe", "-r", "bcache"])
                for partition in partitions:
                    wipe_volume(partition, "superblock")
                util.subp(["modprobe", "bcache"])
            # Stop any mdadm devices, we need to partprobe first to make sure
            # mdadm knows about them
            mdadm_arrays = []
            util.subp(["partprobe", disk])
            (out, _err) = util.subp(["mdadm", "--detail", "--scan"],
                                    capture=True)
            for line in out.splitlines():
                if "ARRAY" in line:
                    mdadm_array = line.rsplit()[1]
                    if mdadm_array not in mdadm_arrays:
                        mdadm_arrays.append(mdadm_array)
            for mdadm_array in mdadm_arrays:
                # Get actual path to blockdev, not the link mdadm gives us
                mdadm_array_path = os.path.realpath(mdadm_array)
                # if these fail its okay, we might not have a running md dev
                # and thats fine
                util.subp(["mdadm", "--stop", mdadm_array_path], rcs=[0, 1])
                util.subp(["mdadm", "--remove", mdadm_array_path], rcs=[0, 1])
            for partition in partitions:
                wipe_volume(partition, "superblock")

        except (parted.DiskLabelException, parted.DiskException):
            # We might be trying to wipe a disk that doesn't have anything on
            # it. If that's the case, there's no reason to fail, just keep
            # going
            pass

        wipe_volume(disk, info.get('wipe'))

    # Create partition table on disk
    if info.get('ptable'):
        # Get device and disk using parted using appropriate partition table
        pdev = parted.getDevice(disk)
        pdisk = parted.freshDisk(pdev, ptable)
        LOG.info("labeling device: '%s' with '%s' partition table", disk,
                 ptable)
        pdisk.commit()


def partition_handler(info, storage_config):
    device = info.get('device')
    size = info.get('size')
    flag = info.get('flag')
    partnumber = info.get('number')
    if not device:
        raise ValueError("device must be set for partition to be created")
    if not size:
        raise ValueError("size must be specified for partition to be created")

    # Find device to attach to in storage_config
    disk = get_path_to_storage_volume(device, storage_config)
    pdev = parted.getDevice(disk)
    pdisk = parted.newDisk(pdev)

    if not partnumber:
        partnumber = len(pdisk.partitions) + 1

    # Offset is either 1 sector after last partition, or near the beginning if
    # this is the first partition
    if partnumber > 1:
        ppartitions = pdisk.partitions
        try:
            previous_partition = ppartitions[partnumber - 2]
        except IndexError:
            raise ValueError(
                "partition numbered '%s' does not exist, so cannot create \
                '%s'. Make sure partitions are in order in config." %
                (partnumber - 1, info.get('id')))
        if previous_partition.type == parted.PARTITION_EXTENDED:
            offset_sectors = previous_partition.geometry.start + 1
        elif previous_partition.type == parted.PARTITION_LOGICAL:
            offset_sectors = previous_partition.geometry.end + 2
        else:
            offset_sectors = previous_partition.geometry.end + 1
    else:
        if storage_config.get(device).get('ptable') == "msdos":
            offset_sectors = 2048
        else:
            offset_sectors = parted.sizeToSectors(
                16, 'KiB', pdisk.device.sectorSize) + 2

    length_sectors = parted.sizeToSectors(int(size.strip(
        string.ascii_letters)), size.strip(string.digits),
        pdisk.device.sectorSize)

    # Handle preserve flag
    if info.get('preserve'):
        partition = pdisk.getPartitionByPath(
            get_path_to_storage_volume(info.get('id'), storage_config))
        if partition.geometry.start != offset_sectors or \
                partition.geometry.length != length_sectors:
            raise ValueError("partition '%s' does not match what exists on \
                disk, but preserve is set to true, bailing" % info.get('id'))
        return
    elif storage_config.get(device).get('preserve'):
        raise NotImplementedError("Partition '%s' is not marked to be \
            preserved, but device '%s' is. At this time, preserving devices \
            but not also the partitions on the devices is not supported, \
            because of the possibility of damaging partitions intended to be \
            preserved." % (info.get('id'), device))

    # Figure out partition type
    if flag == "extended":
        partition_type = parted.PARTITION_EXTENDED
    elif flag == "logical":
        partition_type = parted.PARTITION_LOGICAL
    else:
        partition_type = parted.PARTITION_NORMAL

    # Make geometry and partition
    geometry = parted.Geometry(device=pdisk.device, start=offset_sectors,
                               length=length_sectors)
    partition = parted.Partition(disk=pdisk, type=partition_type,
                                 geometry=geometry)
    constraint = parted.Constraint(exactGeom=partition.geometry)

    # Set flag
    flags = {"boot": parted.PARTITION_BOOT,
             "lvm": parted.PARTITION_LVM,
             "raid": parted.PARTITION_RAID,
             "bios_grub": parted.PARTITION_BIOS_GRUB,
             "prep": parted.PARTITION_PREP}

    if flag:
        if flag in flags:
            partition.setFlag(flags[flag])
        elif flag not in ["extended", "logical"]:
            raise ValueError("invalid partition flag '%s'" % flag)

    # Add partition to disk and commit changes
    LOG.info("adding partition '%s' to disk '%s'" % (info.get('id'), device))
    pdisk.addPartition(partition, constraint)
    pdisk.commit()

    # Wipe the partition if told to do so
    if info.get('wipe') and info.get('wipe') != "none":
        wipe_volume(pdisk.partitions[partnumber - 1].path, info.get('wipe'))


def format_handler(info, storage_config):
    fstype = info.get('fstype')
    volume = info.get('volume')
    part_label = info.get('label')
    if not volume:
        raise ValueError("volume must be specified for partition '%s'" %
                         info.get('id'))

    # Get path to volume
    volume_path = get_path_to_storage_volume(volume, storage_config)

    # Handle preserve flag
    if info.get('preserve'):
        # Volume marked to be preserved, not formatting
        return

    # Generate mkfs command and run
    if fstype in ["ext4", "ext3"]:
        cmd = ['mkfs.%s' % fstype, '-q']
        if part_label:
            if len(part_label) > 16:
                raise ValueError(
                    "ext3/4 partition labels cannot be longer than "
                    "16 characters")
            else:
                cmd.extend(["-L", part_label])
        cmd.append(volume_path)
    elif fstype in ["fat12", "fat16", "fat32", "fat"]:
        cmd = ["mkfs.fat"]
        fat_size = fstype.strip(string.ascii_letters)
        if fat_size in ["12", "16", "32"]:
            cmd.extend(["-F", fat_size])
            if part_label:
                if len(part_label) > 11:
                    raise ValueError(
                        "fat partition names cannot be longer than "
                        "11 characters")
                cmd.extend(["-n", part_label])
        cmd.append(volume_path)
    elif fstype == "swap":
        cmd = ["mkswap", volume_path]
    else:
        # See if mkfs.<fstype> exists. If so try to run it.
        try:
            util.subp(["which", "mkfs.%s" % fstype])
            cmd = ["mkfs.%s" % fstype, volume_path]
        except util.ProcessExecutionError:
            raise ValueError("fstype '%s' not supported" % fstype)
    LOG.info("formatting volume '%s' with format '%s'" % (volume_path, fstype))
    logtime(' '.join(cmd), util.subp, cmd)


def mount_handler(info, storage_config):
    state = util.load_command_environment()
    path = info.get('path')
    filesystem = storage_config.get(info.get('device'))
    if not path and filesystem.get('fstype') != "swap":
        raise ValueError("path to mountpoint must be specified")
    volume = storage_config.get(filesystem.get('volume'))

    # Get path to volume
    volume_path = get_path_to_storage_volume(filesystem.get('volume'),
                                             storage_config)

    if filesystem.get('fstype') != "swap":
        # Figure out what point should be
        while len(path) > 0 and path[0] == "/":
            path = path[1:]
        mount_point = os.path.join(state['target'], path)

        # Create mount point if does not exist
        util.ensure_dir(mount_point)

        # Mount volume
        util.subp(['mount', volume_path, mount_point])

    # Add volume to fstab
    if state['fstab']:
        with open(state['fstab'], "a") as fp:
            if volume.get('type') in ["raid", "bcache", "lvm_partition"]:
                location = get_path_to_storage_volume(volume.get('id'),
                                                      storage_config)
            elif volume.get('type') in ["partition", "dm_crypt"]:
                location = "UUID=%s" % block.get_volume_uuid(volume_path)
            else:
                raise ValueError("cannot write fstab for volume type '%s'" %
                                 volume.get("type"))

            if filesystem.get('fstype') == "swap":
                path = "none"
                options = "sw"
            else:
                path = "/%s" % path
                options = "defaults"

            if filesystem.get('fstype') in ["fat", "fat12", "fat16", "fat32",
                                            "fat64"]:
                fstype = "vfat"
            else:
                fstype = filesystem.get('fstype')
            fp.write("%s %s %s %s 0 0\n" % (location, path, fstype, options))
    else:
        LOG.info("fstab not in environment, so not writing")


def lvm_volgroup_handler(info, storage_config):
    devices = info.get('devices')
    device_paths = []
    name = info.get('name')
    if not devices:
        raise ValueError("devices for volgroup '%s' must be specified" %
                         info.get('id'))
    if not name:
        raise ValueError("name for volgroups needs to be specified")

    for device_id in devices:
        device = storage_config.get(device_id)
        if not device:
            raise ValueError("device '%s' could not be found in storage config"
                             % device_id)
        device_paths.append(get_path_to_storage_volume(device_id,
                            storage_config))

    # Handle preserve flag
    if info.get('preserve'):
        # LVM will probably be offline, so start it
        util.subp(["vgchange", "-a", "y"])
        # Verify that volgroup exists and contains all specified devices
        current_paths = []
        (out, _err) = util.subp(["pvdisplay", "-C", "--separator", "=", "-o",
                                "vg_name,pv_name", "--noheadings"],
                                capture=True)
        for line in out.splitlines():
            if name in line:
                current_paths.append(line.split("=")[-1])
        if set(current_paths) != set(device_paths):
            raise ValueError("volgroup '%s' marked to be preserved, but does \
                             not exist or does not contain the right physical \
                             volumes" % info.get('id'))
    else:
        # Create vgrcreate command and run
        cmd = ["vgcreate", name]
        cmd.extend(device_paths)
        util.subp(cmd)


def lvm_partition_handler(info, storage_config):
    volgroup = storage_config.get(info.get('volgroup')).get('name')
    name = info.get('name')
    if not volgroup:
        raise ValueError("lvm volgroup for lvm partition must be specified")
    if not name:
        raise ValueError("lvm partition name must be specified")

    # Handle preserve flag
    if info.get('preserve'):
        (out, _err) = util.subp(["lvdisplay", "-C", "--separator", "=", "-o",
                                "lv_name,vg_name", "--noheadings"],
                                capture=True)
        found = False
        for line in out.splitlines():
            if name in line:
                if volgroup == line.split("=")[-1]:
                    found = True
                    break
        if not found:
            raise ValueError("lvm partition '%s' marked to be preserved, but \
                             does not exist or does not mach storage \
                             configuration" % info.get('id'))
    elif storage_config.get(info.get('volgroup')).get('preserve'):
        raise NotImplementedError("Lvm Partition '%s' is not marked to be \
            preserved, but volgroup '%s' is. At this time, preserving \
            volgroups but not also the lvm partitions on the volgroup is \
            not supported, because of the possibility of damaging lvm \
            partitions intended to be preserved." % (info.get('id'), volgroup))
    else:
        cmd = ["lvcreate", volgroup, "-n", name]
        if info.get('size'):
            cmd.extend(["-L", info.get('size')])
        else:
            cmd.extend(["-l", "100%FREE"])

        util.subp(cmd)

    if info.get('ptable'):
        raise ValueError("Partition tables on top of lvm logical volumes is \
                         not supported")


def dm_crypt_handler(info, storage_config):
    state = util.load_command_environment()
    volume = info.get('volume')
    key = info.get('key')
    keysize = info.get('keysize')
    cipher = info.get('cipher')
    dm_name = info.get('dm_name')
    if not volume:
        raise ValueError("volume for cryptsetup to operate on must be \
            specified")
    if not key:
        raise ValueError("encryption key must be specified")
    if not dm_name:
        dm_name = info.get('id')

    volume_path = get_path_to_storage_volume(volume, storage_config)

    # TODO: this is insecure, find better way to do this
    tmp_keyfile = tempfile.mkstemp()[1]
    fp = open(tmp_keyfile, "w")
    fp.write(key)
    fp.close()

    cmd = ["cryptsetup"]
    if cipher:
        cmd.extend(["--cipher", cipher])
    if keysize:
        cmd.extend(["--key-size", keysize])
    cmd.extend(["luksFormat", volume_path, tmp_keyfile])

    util.subp(cmd)

    cmd = ["cryptsetup", "open", "--type", "luks", volume_path, dm_name,
           "--key-file", tmp_keyfile]

    util.subp(cmd)

    os.remove(tmp_keyfile)

    # A crypttab will be created in the same directory as the fstab in the
    # configuration. This will then be copied onto the system later
    if state['fstab']:
        crypt_tab_location = os.path.join(os.path.split(state['fstab'])[0],
                                          "crypttab")
        uuid = block.get_volume_uuid(volume_path)
        with open(crypt_tab_location, "a") as fp:
            fp.write("%s UUID=%s none luks\n" % (dm_name, uuid))
    else:
        LOG.info("fstab configuration is not present in environment, so \
            cannot locate an appropriate directory to write crypttab in \
            so not writing crypttab")


def raid_handler(info, storage_config):
    state = util.load_command_environment()
    devices = info.get('devices')
    raidlevel = info.get('raidlevel')
    spare_devices = info.get('spare_devices')
    if not devices:
        raise ValueError("devices for raid must be specified")
    if raidlevel not in [0, 1, 5]:
        raise ValueError("invalid raidlevel '%s'" % raidlevel)

    device_paths = list(get_path_to_storage_volume(dev, storage_config) for
                        dev in devices)

    if spare_devices:
        spare_device_paths = list(get_path_to_storage_volume(dev,
                                  storage_config) for dev in spare_devices)

    cmd = ["yes", "|", "mdadm", "--create", "/dev/%s" % info.get('id'),
           "--level=%s" % raidlevel, "--raid-devices=%s" % len(device_paths)]

    for device in device_paths:
        # Zero out device superblock just in case device has been used for raid
        # before, as this will cause many issues
        util.subp(["mdadm", "--zero-superblock", device])

        cmd.append(device)

    if spare_devices:
        cmd.append("--spare-devices=%s" % len(spare_device_paths))
        for device in spare_device_paths:
            util.subp(["mdadm", "--zero-superblock", device])

            cmd.append(device)

    # Create the raid device
    util.subp(" ".join(cmd), shell=True)

    # A mdadm.conf will be created in the same directory as the fstab in the
    # configuration. This will then be copied onto the installed system later.
    # The file must also be written onto the running system to enable it to run
    # mdadm --assemble and continue installation
    if state['fstab']:
        mdadm_location = os.path.join(os.path.split(state['fstab'])[0],
                                      "mdadm.conf")
        (out, _err) = util.subp(["mdadm", "--detail", "--scan"], capture=True)
        with open(mdadm_location, "w") as fp:
            fp.write(out)
    else:
        LOG.info("fstab configuration is not present in the environment, so \
            cannot locate an appropriate directory to write mdadm.conf in, \
            so not writing mdadm.conf")

    # If ptable is specified, call disk_handler on this mdadm device to create
    # the table
    if info.get('ptable'):
        disk_handler(info, storage_config)


def bcache_handler(info, storage_config):
    backing_device = get_path_to_storage_volume(info.get('backing_device'),
                                                storage_config)
    cache_device = get_path_to_storage_volume(info.get('cache_device'),
                                              storage_config)
    if not backing_device or not cache_device:
        raise ValueError("backing device and cache device for bcache must be \
                specified")

    # The bcache module is not loaded when bcache is installed by apt-get, so
    # we will load it now
    util.subp(["modprobe", "bcache"])

    # If both the backing device and cache device are specified at the same
    # time than it is not necessary to attach the cache device manually, as
    # bcache will do this automatically.
    util.subp(["make-bcache", "-B", backing_device, "-C", cache_device])

    # Some versions of bcache-tools will register the bcache device as soon as
    # we run make-bcache, on older versions we need to register it manually
    # though
    try:
        get_path_to_storage_volume(info.get('id'), storage_config)
    except OSError:
        # Register
        for path in [backing_device, cache_device]:
            fp = open("/sys/fs/bcache/register", "w")
            fp.write(path)
            fp.close()

    if info.get('ptable'):
        raise ValueError("Partition tables on top of lvm logical volumes is \
                         not supported")


def install_missing_packages_for_meta_custom():
    """Install all the missing package that `meta_custom` requires to
    function properly."""
    missing_packages = [
        package
        for package in CUSTOM_REQUIRED_PACKAGES
        if not util.has_pkg_installed(package)
    ]
    if len(missing_packages) > 0:
        util.apt_update()
        util.install_packages(missing_packages)


def meta_custom(args):
    """Does custom partitioning based on the layout provided in the config
    file. Section with the name storage contains information on which
    partitions on which disks to create. It also contains information about
    overlays (raid, lvm, bcache) which need to be setup.
    """

    # make sure parted has been imported
    if "parted" not in sys.modules:
        raise ImportError("module parted is not available but is needed to \
                          run meta_custom")

    command_handlers = {
        'disk': disk_handler,
        'partition': partition_handler,
        'format': format_handler,
        'mount': mount_handler,
        'lvm_volgroup': lvm_volgroup_handler,
        'lvm_partition': lvm_partition_handler,
        'dm_crypt': dm_crypt_handler,
        'raid': raid_handler,
        'bcache': bcache_handler
    }

    state = util.load_command_environment()
    cfg = util.load_command_config(args, state)

    storage_config = cfg.get('storage', [])
    if not storage_config:
        raise Exception("storage configuration is required by mode '%s' "
                        "but not provided in the config file" % CUSTOM)

    # make sure the required packages are installed
    install_missing_packages_for_meta_custom()

    # Since storage config will often have to be searched for a value by its
    # id, and this can become very inefficient as storage_config grows, a dict
    # will be generated with the id of each component of the storage_config as
    # its index and the component of storage_config as its value
    storage_config_dict = OrderedDict((d["id"], d) for (i, d) in
                                      enumerate(storage_config))

    for command in storage_config:
        handler = command_handlers.get(command['type'])
        if not handler:
            raise ValueError("unknown command type '%s'" % command['type'])
        try:
            handler(command, storage_config_dict)
        except Exception as error:
            LOG.error("An error occured handling '%s': %s - %s" %
                      (command.get('id'), type(error).__name__, error))
            raise

    return 0


def meta_simple(args):
    """Creates a root partition. If args.mode == SIMPLE_BOOT, it will also
    create a separate /boot partition.
    """
    state = util.load_command_environment()

    cfg = util.load_command_config(args, state)

    if args.target is not None:
        state['target'] = args.target

    if state['target'] is None:
        sys.stderr.write("Unable to find target.  "
                         "Use --target or set TARGET_MOUNT_POINT\n")
        sys.exit(2)

    devices = args.devices
    if devices is None:
        devices = cfg.get('block-meta', {}).get('devices', [])

    bootpt = get_bootpt_cfg(
        cfg.get('block-meta', {}).get('boot-partition', {}),
        enabled=args.mode == SIMPLE_BOOT, fstype=args.boot_fstype,
        root_fstype=args.fstype)

    ptfmt = get_partition_format_type(cfg.get('block-meta', {}))

    # Remove duplicates but maintain ordering.
    devices = list(OrderedDict.fromkeys(devices))

    # Multipath devices might be automatically assembled if multipath-tools
    # package is available in the installation environment. We need to stop
    # all multipath devices to exclusively use one of paths as a target disk.
    block.stop_all_unused_multipath_devices()

    if len(devices) == 0:
        devices = block.get_installable_blockdevs()
        LOG.warn("'%s' mode, no devices given. unused list: %s",
                 args.mode, devices)

    if len(devices) > 1:
        if args.devices is not None:
            LOG.warn("'%s' mode but multiple devices given. "
                     "using first found", args.mode)
        available = [f for f in devices
                     if block.is_valid_device(f)]
        target = sorted(available)[0]
        LOG.warn("mode is '%s'. multiple devices given. using '%s' "
                 "(first available)", args.mode, target)
    else:
        target = devices[0]

    if not block.is_valid_device(target):
        raise Exception("target device '%s' is not a valid device" % target)

    (devname, devnode) = block.get_dev_name_entry(target)

    LOG.info("installing in '%s' mode to '%s'", args.mode, devname)

    sources = cfg.get('sources', {})
    dd_images = util.get_dd_images(sources)

    if len(dd_images):
        # we have at least one dd-able image
        # we will only take the first one
        rootdev = write_image_to_disk(dd_images[0], devname)
        util.subp(['mount', rootdev, state['target']])
        return 0

    # helper partition will forcibly set up partition there
    ptcmd = ['partition', '--format=' + ptfmt]
    if bootpt['enabled']:
        ptcmd.append('--boot')
    ptcmd.append(devnode)

    if bootpt['enabled'] and ptfmt in ("uefi", "prep"):
        raise ValueError("format=%s with boot partition not supported" % ptfmt)

    bootdev_ptnum = None
    rootdev_ptnum = None
    bootdev = None
    if bootpt['enabled']:
        bootdev_ptnum = 1
        rootdev_ptnum = 2
    else:
        if ptfmt == "prep":
            rootdev_ptnum = 2
        else:
            rootdev_ptnum = 1

    logtime("creating partition with: %s" % ' '.join(ptcmd),
            util.subp, ptcmd)

    ptpre = ""
    if not os.path.exists("%s%s" % (devnode, rootdev_ptnum)):
        # perhaps the device is /dev/<blockname>p<ptnum>
        if os.path.exists("%sp%s" % (devnode, rootdev_ptnum)):
            ptpre = "p"
        else:
            LOG.warn("root device %s%s did not exist, expecting failure",
                     devnode, rootdev_ptnum)

    if bootdev_ptnum:
        bootdev = "%s%s%s" % (devnode, ptpre, bootdev_ptnum)
    rootdev = "%s%s%s" % (devnode, ptpre, rootdev_ptnum)

    LOG.debug("rootdev=%s bootdev=%s fmt=%s bootpt=%s",
              rootdev, bootdev, ptfmt, bootpt)

    # mkfs for root partition first and mount
    cmd = ['mkfs.%s' % args.fstype, '-q', '-L', 'cloudimg-rootfs', rootdev]
    logtime(' '.join(cmd), util.subp, cmd)
    util.subp(['mount', rootdev, state['target']])

    if bootpt['enabled']:
        # create 'boot' directory in state['target']
        boot_dir = os.path.join(state['target'], 'boot')
        util.subp(['mkdir', boot_dir])
        # mkfs for boot partition and mount
        cmd = ['mkfs.%s' % bootpt['fstype'],
               '-q', '-L', bootpt['label'], bootdev]
        logtime(' '.join(cmd), util.subp, cmd)
        util.subp(['mount', bootdev, boot_dir])

    if state['fstab']:
        with open(state['fstab'], "w") as fp:
            if bootpt['enabled']:
                fp.write("LABEL=%s /boot %s defaults 0 0\n" %
                         (bootpt['label'], bootpt['fstype']))
            fp.write("LABEL=%s / %s defaults 0 0\n" %
                     ('cloudimg-rootfs', args.fstype))
    else:
        LOG.info("fstab not in environment, so not writing")

    return 0


def POPULATE_SUBCMD(parser):
    populate_one_subcmd(parser, CMD_ARGUMENTS, block_meta)

# vi: ts=4 expandtab syntax=python
