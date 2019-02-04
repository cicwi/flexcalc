# >>> Imports >>>

import numpy
import gc
import os
import re
import warnings
import pickle
import time
from copy import deepcopy

#from glob import glob        

from flexdata import io
from flexdata import array
from flexdata import display
from flextomo import project
from flexcalc import process

import networkx
import matplotlib.pyplot as plt

# >>> Classes >>>

class logger:
   """
   A class for logging and printing messages.
   """
   #def __init__(self):
   #   pass
   
   @staticmethod
   def print(message):
      """
      Simply prints and saves a message.
      """
      print(message)   

   @staticmethod
   def title(message):
      """
      Print something important.
      """
      print('')
      print(message)   
      print('')

   @staticmethod
   def warning(message):
      """
      Raise a warning.
      """
      warnings.warn(message)
      
   @staticmethod   
   def error(message):
      """
      Raise an error.
      """
      raise Exception(message)
         
class Buffer:
    """
    Each node has an input and output buffer. It will be in read-only or write-only state.
    Buffer can store a memmap data and a metadata record.
    """

    def __init__(self, path, writer_node, shape = (1, 1, 1), dtype = 'float32'):
        """
        Initialize buffer.
        """
        # Find an appropriate the directory:
        if not os.path.exists(path): os.mkdir(path)
        
        # Don't assign a file before buffer is activated!
        self.filename = ''
        self.path = path
        
        # Init data and meta:
        self._data_ = None
        self._meta_ = None
                
        # Shape and type of the memmap data:
        self.shape = shape
        self.dtype = dtype
        
        # Init links to the writer/reader nodes:
        self.writer_node = writer_node
        self.reader_node = None
        
        self.readonly = False
        
        #logger.print(writer_node.node_name + ' created a buffer!')
        
    def __copy__(self):
        logger.print('Copying a buffer!')
        
        return None
        
    def _get_filename_(self):
        """
        Find an unused filename:
        """
        if self.filename: 
            if os.path.exists(self.filename): return None
        
        # Create a dir if needed:
        if not os.path.exists(self.path): os.mkdir(self.path)    
        
        # Get all files to add one at the end of the list:
        files = io.get_files_sorted(self.path, 'scratch')
      
        # Get the new index:
        if files == []:
            index = 0
        else:
            exist = [int(re.findall('\d+', f)[-1]) for f in files]
            index = min([ii for ii in numpy.arange(9999) if ii not in exist])
            
        # Add new file:
        self.filename = os.path.join(self.path, 'scratch_%04u' % index)
        
    def switch_readonly(self):
        """
        Switch the buffer into reading mode.
        """
        # Get a new name if needed:
        self._get_filename_()
        
        # Make sure that file was written on disk:
        if self._data_ is not None:
            self._data_.flush()              
  
        # Link with a file (array can be modified but the file stays read-only):
        self._data_ = numpy.memmap(self.filename, dtype = self.dtype, mode = 'c', shape = self.shape) 
        self.readonly = True
            
    def switch_writeonly(self):
      """
      Switch the buffer into writing mode.
      """
      # Get a new name if needed:
      self._get_filename_()
      
      # Link with a file:
      self._data_ = numpy.memmap(self.filename, dtype = self.dtype, mode = 'w+', shape = self.shape)       
      self.readonly = False
    
    def suicide(self):
      """
      Remove file from disk and delete variables.
      """
      self._data_ = None
      self._meta_ = None
      gc.collect()
        
      if os.path.exists(self.filename):     
          os.remove(self.filename)
          logger.print('Deleted a memmap file @' + self.filename)
      
      self.filename = ''
      
    def set_shape(self, shape):
        """
        Change the shape of the buffered data.
        """
        if self.readonly: logger.error('Attempt to write into read-only block!')
        
        if any(self.shape != shape): 
            self.shape = tuple(shape)
        
        self._data_ = numpy.memmap(self.filename, dtype = self.dtype, mode = 'w+', shape = self.shape)
        
    def set_data(self, data):
        '''
        Write the data data.
        '''
        if self.readonly: logger.error('Attempt to write into read-only block!')
        
        if self.shape != data.shape: self.shape = data.shape
        if self.dtype != data.dtype: self.dtype = data.dtype
        #logger.error('Wrong data data shape:' + str(data.shape))
        
        # Check free space:
        buffer_gb = data.nbytes / 1e9 
        free_gb = array.free_disk(self.filename)
        logger.print('Writing buffer of %1.1fGB (%u%% of current disk space).' % (buffer_gb, 100 * buffer_gb / free_gb))
        
        # We will open data here again in case the shape or type changed:
        self._data_ = numpy.memmap(self.filename, dtype = self.dtype, mode = 'w+', shape = self.shape)  
        self._data_[:] = data
        self._data_.flush()
    
    def get_data(self):
        '''
        Read the data data.
        '''
        if self._data_ is None:
            logger.error('Attempt to read an empty buffer!')
        
        # Check free space:        
        buffer_gb = self._data_.nbytes / 1e9 
        free_gb = array.free_memory(False)                
        logger.print('Retrieving buffer of %1.1fGB (%u%% of current RAM).' % (buffer_gb, 100 * buffer_gb / free_gb))
        
        return self._data_
    
    def set_meta(self, meta):
        '''
        Write the meta record.
        '''
        if self.readonly: logger.error('Attempt to write into read-only block!')
        self._meta_ = meta
        
    def get_meta(self):
        '''
        Read the meta record.
        '''
        if self.readonly:
           return deepcopy(self._meta_)
       
        else:
           return self._meta_
    
    def __del__(self):
        # This can be a copy of an original buffer. Suicide only on request!
        #self.suicide()
        pass
      
# States:
_NSTATE_PENDING_ = 0
_NSTATE_ACTIVE_ = 1
_NSTATE_DEACTIVATED_ = 2

# Types of nodes:
_NTYPE_BATCH_ = 0
_NTYPE_GROUP_ = 1
   
class Node:
   """
   Class responsible for processing of a single block of data.
   It has two buffers: input and output.
   Three states: waiting, active, ready
   Three methods: start, action, finish
   """
   # Default node type:
   node_type = _NTYPE_BATCH_
   node_name = 'Default node'
   
   def __init__(self, pipe, arguments, inputs):
      """
      Initialize ...
      """
      # Buffers:
      self.inputs = inputs
      self.outputs = []
      
      # Parent:
      self.pipe = pipe
      
      # Initial state and type:
      self.state = _NSTATE_PENDING_
       
      # Arguments:
      self.arguments = arguments
            
      logger.print('Initializing node: ' + self.node_name)
      
      # Call the initialize method defined in the sub-class:
      self.initialize()
      
   def state2str(self):
       """
       Report my state.
       """
       if self.state == _NSTATE_PENDING_: return 'PENDING'
       elif self.state == _NSTATE_ACTIVE_: return 'ACTIVE'
       elif self.state == _NSTATE_DEACTIVATED_: return 'DEACTIVATED'
       else: return 'UNKNOWN'
   
   def initialize(self):
       """
       Initializtion callback. Override this in sub-classes
       """
       self.init_outputs(1) 
    
   def runtime(self):
       """
       Runtime callback. Override this in sub-classes
       """
       # Pass data from the input buffer to output without change:
       data, meta = self.get_input(0)
       self.set_output(data, meta, 0)
       
   def init_outputs(self, count):
      """
      Create output buffers.
      """
      
      for ii in range(count):
          buffer = Buffer(self.pipe._scratch_path_, self)
          
          # By default, buffer meta record is passed from parent to child:
          if len(self.inputs) > 0:
              meta = self.inputs[0].get_meta()
              buffer.set_meta(meta)
              
          self.outputs.append(buffer)   
      
   def set_output(self, data = None, meta = None, index = 0):
      """
      Set the writable buffer data and meta.
      """
      buffer = self.outputs[index]        
        
      if data is not None:
          buffer.set_data(data)
          
      if meta is not None:    
          buffer.set_meta(meta)
      
   def get_input(self, index = 0):
      """
      Get the readable buffer data and meta.
        
        Returns:
            data, meta
      """
      # TODO: get meta too!!!
      data = self.inputs[index].get_data()
      meta = self.inputs[index].get_meta()
        
      return data, meta
  
   def get_output(self, index = 0):
      """
      Get the outputs data and meta.
        
        Returns:
            data, meta
      """
      # TODO: get meta too!!!
      data = self.outputs[index].get_data()
      meta = self.outputs[index].get_meta()
        
      return data, meta
  
   def get_parents(self):
        """
        Get parent nodes.
        """
        nodes = []
        
        for buffer in self.inputs:
            node = buffer.writer_node
            nodes.append(node)
        
        return nodes 

   def isready(self):       
      """
      Check if the node is ready.
      """
      # Check if parent nodes are deactivated:
      for node in self.get_parents():       
          if node.state != _NSTATE_DEACTIVATED_:
              return False
      
          if self.state != _NSTATE_PENDING_:
              return False
        
      return True
                     
   def activate(self):
      """
      Switch input buffers to read-only mode and output buffers to write-only.
      """
      logger.title('Activating node: ' + self.node_name)
      
      # Check if parent nodes are deactivated:
      if not self.isready():
              logger.error('Attempt to activate a wrong node. Check node state and parent nodes states.')
               
      # Switch buffers to read/write:        
      for buffer in self.inputs:
         buffer.switch_readonly()
      
      for buffer in self.outputs:
         buffer.switch_writeonly()
         
      # Change state and run:
      self.state = _NSTATE_ACTIVE_
      self.runtime()
      
      # If success, change state:
      self.deactivate()
                              
   def deactivate(self):
      """
      Kill all inputs. Switch all outputs to read.
      """
      # Check if the node is in correct state:
      if self.state != _NSTATE_ACTIVE_:
          logger.error('Attempt to deactivate node that is not on an active state!')
        
      # Clean up memory!  
      gc.collect()  
        
      self.state = _NSTATE_DEACTIVATED_
      
      # Check if the output buffer is
      if numpy.prod(self.outputs[0].shape) < 1000:
          logger.warning('Node output buffer is too small!')
      
      # Switch states of the buffers:
      for buffer in self.outputs:
         buffer.switch_readonly()
            
      for buffer in self.inputs:
         buffer.suicide()

   def get_children(self):
       """
       Get children nodes.
       """
       nodes = []
        
       if self.outputs != []: 
           for buffer in self.outputs:
               node = buffer.reader_node
               
               if node is not None:
                   if node.node_type == _NTYPE_GROUP_:
                       # If nodes is a group node, all buffers end up in a single node:
                       return [node, ]
                   
                   else:
                       nodes.append(node)
               
           return nodes
       
       else:
           return []
     
   def cleanup(self):
       '''
       Remove files:
       '''
       # Delete files:
       for buffer in self.outputs:
           buffer.suicide()
           
       for buffer in self.inputs:
           buffer.suicide()      
           
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> Node classes >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>           
       
class batch_node(Node):
    """
    A standard batch node based on a given callback function.
    Callback function is saved as the first argument in the argument list.
    """      
    node_name = 'batch'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
        
        callback = self.arguments[0]
        args = self.arguments[1]
        
        if data != []:
            out = callback(data, **args)
            
            # If there is output pass it further down the pipeline:
            if out != None:
                self.set_output(out, meta, 0)
                
            else:
                self.set_output(data, meta, 0)

class info_node(Node):
    """
    Print data info.
    """      
    node_name = 'Buffer Info'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        logger.title('Found %u buffers.' % len(self.inputs))
        
        for ii in range(len(self.inputs)):
            data, meta = self.get_input(ii)
        
            logger.print('Data shape: ' + str(data.shape))
            logger.print('Data range: ' + str([data.min(), data.max()]))
      
            logger.print('Meta:')
            logger.print(meta)
            
            self.set_output(data, meta, ii) 
                                
class fdk_node(Node):
    """
    Feldkamp reconstruction.
    """      
    node_name = 'FDK'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
        
        vol_shape = self.arguments[0]
        
        if vol_shape:
            vol = numpy.zeros(vol_shape, dtype = 'float32')
            
        else:
            vol = project.init_volume(data, meta['geometry'])
        
        project.settings['block_number'] = 20
        project.FDK(data, vol, meta['geometry'])
        
        self.set_output(vol, meta, 0)                

class crop_node(Node):
    """
    Apply crop.
    """      
    node_name = 'Crop'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
        
        (dim, width) = self.arguments
               
        data = array.crop(data, dim, width, meta['geometry'])
  
        self.set_output(data, meta, 0) 
        
class bin_node(Node):
    """
    Apply binning.
    """      
    node_name = 'Bin'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
        
        dim = self.arguments[0]
               
        data = array.bin(data, dim)
  
        self.set_output(data, meta, 0)          

class pad_node(Node):
    """
    Apply autocrop.
    """      
    node_name = 'Pad'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
        
        (width, dim, mode) = self.arguments
        
        data = array.pad(data, dim, width, mode, meta['geometry'])
        
        self.set_output(data, meta, 0)               

class beamhardening_node(Node):
    """
    Apply beam hardening based on a single material approximation and an estimated spectrum.
    """      
    
    node_name = 'Beam-hardening correction'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
        
        (file, compound, density) = self.arguments
                
        # Use toml files:
        if os.path.exists(file):
            spec = io.read_toml(file)
            
        else:
            raise Exception('File not found:' + file)
        
        data = process.equivalent_density(data, meta, spec['energy'], spec['spectrum'], compound = compound, density = density)
                
        self.set_output(data, meta, 0)     

class display_node(Node):
    """
    Display data.
    """      
    node_name = 'Display'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
        
        display_type = self.arguments[0]
        args = self.arguments[1]
        
        # Find callback:        
        dictionary = {'slice': display.slice, 'max_projection': display.max_projection,'min_projection':display.min_projection,'pyqt_graph':display.pyqt_graph}
        callback = dictionary[display_type]        
        
        callback(data, **args)
        
        self.set_output(data, meta, 0)            

class autocrop_node(Node):
    """
    Apply autocrop.
    """      
    node_name = 'Autocrop'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
                        
        a,b,c = process.bounding_box(data)
        
        sz = data.data.shape
        
        logger.print('Bounding box found: ' + str([a,b,c]))
        logger.print('Old dimensions are: ' + str(sz))
        
        geometry = meta['geometry']
        
        data = array.crop(data, 0, [a[0], sz[0] - a[1]], geometry)
        data = array.crop(data, 1, [b[0], sz[1] - b[1]], geometry)
        data = array.crop(data, 2, [c[0], sz[2] - c[1]], geometry)
        
        logger.print('New dimensions are: ' + str(data.shape))
        
        self.set_output(data, meta, 0)                

class markernorm_node(Node):
    """
    Find marker and normalize data using its intensity.
    """      
    node_name = 'Marker normalization'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
         
        norm, size = self.arguments[0]               
        
        # Find the marker:
        a,b,c = process.find_marker(data, meta, size)    
        
        rho = data[a-1:a+1, b-1:b+1, c-1:c+1].mean()
    
        logger.print('Marker density is: %2.2f' % rho)
        
        if abs(rho - norm) / rho > 0.2:
            logger.warning('Suspicious marker density: %0.2f. Will not apply correction!' % rho)
            
        else:
            data *= (norm / rho)
        
        self.set_output(data, meta, 0)  
        
class threshold_node(Node):
    """
    Apply a soft threshold.
    """      
    node_name = 'Soft threshold'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
        
        (mode, threshold) = self.arguments
        
        process.soft_threshold(data, mode, threshold)

        self.set_output(data, meta, 0)                        
        
class cast2type_node(Node):
    """
    Apply autocrop.
    """      
    node_name = 'Cast to type'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
        
        (dtype, bounds) = self.arguments
        
        logger.print('Casting data to ' + str(dtype))
        
        data = array.cast2type(data, dtype, bounds)
        
        self.set_output(data, meta, 0)  
        
class flatlog_node(Node):
    """
    Apply flat-field and dark-field correction. Take -log(x).
    """      
    node_name = 'Flatlog'
    node_type = _NTYPE_BATCH_
                  
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
        
        (usemax, flats, darks, sample, flipdim) = self.arguments
        
        if usemax:
            # Use data-driven flat field correction:
            data = process.flatfield(data)
            
        else:
            
            path = meta.get('path')
            if path == []:
                logger.error('Path to data not found in the metadata.')
            
            # Read darks and flats:
            if darks:
                dark = io.read_tiffs(path, darks, sample, sample)
                    
                if dark.ndim > 2:
                    dark = dark.mean(0)
                    
                data = (data - dark[:, None, :])
                
            else:
                dark = 0
                
            if flats:    
                flat = io.read_tiffs(path, flats, sample, sample)
                if flat.ndim > 2:
                    flat = flat.mean(0)
                
                if flipdim:
                    data = data / (flat - dark)[::-1, None, :]
                else:
                    data = data / (flat - dark)[:, None, :]
            
        data = -numpy.log(data).astype('float32')
        
        # Fix nans and infs after log:
        data[~numpy.isfinite(data)] = 10        
        
        # TODO: make sure that all functions return data!
        self.set_output(data, meta, 0)        
      
class vol_merge_node(Node):
    """
    Merge volumes node.
    """     
    
    node_name = 'Merge volumes'
    node_type = _NTYPE_GROUP_
    
    def initialize(self):
        self.init_outputs(1)
                                
    def runtime(self):
        
        # Determine Total Bounds:
        tot_bounds = numpy.zeros((3, 2))
                
        # Find bounds of the volumes:
        for ii in range(len(self.inputs)):
            
            data, meta = self.get_input(ii)
            geom = meta['geometry']
    
            bounds = numpy.array(data.shape) * geom['img_pixel']
            bounds = numpy.array([geom['vol_tra'] - bounds / 2, geom['vol_tra'] + bounds / 2]).T
            
            tot_bounds[:, 0] = numpy.min([bounds[:, 0], tot_bounds[:, 0]], axis = 0)
            tot_bounds[:, 1] = numpy.max([bounds[:, 1], tot_bounds[:, 1]], axis = 0)
        
        meta = self.inputs[ii].get_meta()
        tot_meta = deepcopy(meta)
        tot_meta['geometry']['vol_tra'] = (tot_bounds[:, 1] + tot_bounds[:, 0]) / 2
        
        tot_bounds = tot_bounds[:, 1] - tot_bounds[:, 0]
        tot_shape = (numpy.ceil(tot_bounds / tot_meta['geometry']['img_pixel'])).astype('int')

        # Update buffer shape and get a link to it:        
        self.outputs[0].set_shape(tot_shape)
        tot_data = self.outputs[0].get_data()
        self.outputs[0].set_meta(tot_meta)
        
        # Append volumes:    
        for ii in range(len(self.inputs)):
            
            data, meta = self.get_input(ii)
            geom = meta['geometry']
            process.append_volume(data, geom, tot_data, tot_meta['geometry'], ramp = data.shape[0]//10)
            
class proj_merge_node(Node):
    """
    Merge projections node.
    """      
    node_name = 'Merge projections'
    node_type = _NTYPE_GROUP_
    
    def initialize(self):
        # The assumption is that all datasets are of the same size and resolution!
        
        # List of geometries and unique source positions:
        geoms_list = []
        src_list = []
        
        # First, we need to check how many unique source positions there are.
        # Create a separate sub-group of geometries for each source position.
        for ii in range(len(self.inputs)):
            
            meta = self.inputs[ii].get_meta()
            
            geom = meta.get('geometry')
            if geom is None:
                logger.error('geometry record not found!')
            
            src = [geom['src_vrt'], geom['src_mag'], geom['src_hrz']]
            
            if src_list is []:
                src_list.append(src)
                geoms_list.append([geom,])
                
            elif src not in src_list:
                src_list.append(src)
                geoms_list.append([geom,])
                
            else:
                index = src_list.index(src)    
                geoms_list[index].append(geom)
        
        # Save geoms_list for runtime:        
        self._geoms_list_ = geoms_list
            
        # Number of outputs = number of unique source positions:
        self.init_outputs(len(self._geoms_list_))
                
    def runtime(self):
        
        data, meta = self.get_input(0)
        shape = data.shape        
        
        # Compute a total geometry for each source position:
        for ii, geoms in enumerate(self._geoms_list_):
            
            # Total geometry and shape for a single unique source position:
            tot_shape, tot_geom = array.tiles_shape(shape, geoms)
                    
            # Create outputs:
            tot_meta = meta.copy()
            tot_meta['geometry'] = tot_geom
            
            self.outputs[ii].set_shape(tot_shape)
            self.outputs[ii].set_meta(tot_meta)
            
        # Retrieve a list of unique sources:
        sources = []
        for ii in range(len(self.outputs)):
            
            meta = self.outputs[ii].get_meta()
            geom = meta['geometry']
            sources.append([geom['src_vrt'], geom['src_mag'], geom['src_hrz']])

        # Add tiles one by one:            
        for ii in range(len(self.inputs)):

            # Find a unique source position:
            data, meta = self.get_input(ii)
            geom = meta['geometry']
            
            src = [geom['src_vrt'], geom['src_mag'], geom['src_hrz']]
            index = sources.index(src)
            
            # Get the corresponding output
            tot_data, tot_meta = self.get_output(index)
            tot_geom = tot_meta['geometry']
            
            # Derotate tile if needed:
            if geom['det_roll'] != 0:
                angle = numpy.rad2deg(geom['det_roll'])
                process.rotate(data, -angle, axis = 1)
                geom['det_roll'] = 0
            
            # Append tile:    
            process.append_tile(data, geom, tot_data, tot_geom)
                                
class optimize_node(Node):
    """
    Use auto-focusing to optimize geometry parameters. Its a group node - it will wait untill all previous nodes are ready before activating.
    """      
    node_name = 'Optimize'
    node_type = _NTYPE_GROUP_
    
    def initialize(self):
        
        # Initialize as many outputs as there are inputs:
        self.init_outputs(len(self.inputs))
            
    def runtime(self):
        
        (values, key, tile_index, sampling, metric) = self.arguments
        
        # Either optimize based on one tile or run all of them.
        for ii in range(len(self.inputs)):
        
            # Read data form a single buffer:
            data, meta = self.get_input(ii)
            
            if (ii == tile_index) | (tile_index is None):
            
                process.optimize_modifier(values + meta['geometry'][key], data, meta['geometry'], samp = sampling, key = key, metric = metric)
            
            self.set_output(data, meta, ii)  
  
class writer_node(Node):
    """
    Write data on disk.
    """      
    node_name = 'Writer'
    node_type = _NTYPE_BATCH_
                   
    def runtime(self):
        
        # Read data form a single buffer:
        data, meta = self.get_input(0)
        
        (folder, name, dim, skip, compress) = self.arguments
            
        path = meta['path']
        
        print('Writing data at:', os.path.join(path, folder))
        io.write_tiffs(os.path.join(path, folder), name, data, dim = dim, skip = skip, zip = compress)
        
        print('Writing meta to:', os.path.join(path, folder, 'meta.toml'))
        io.write_toml(os.path.join(path, folder, 'meta.toml'), meta)  

        self.set_output(data, meta, 0)  
        
class reader_node(Node):
    
    node_name = 'Reader'
    node_type = _NTYPE_BATCH_
    
    def initialize(self):
        '''
        Initiallization callback of read_data. 
        '''
        # Get arguments:
        paths, name, sampling, shape, dtype, format, flipdim, proj_number = self.arguments
        paths = io.get_folders_sorted(paths)
       
        # Create as many output buffers as there are paths:
        self.init_outputs(len(paths))
        
        for ii, path in enumerate(paths):
          
            logger.print('Found data @ ' + path)
            
            shape = io.stack_shape(path, name, sampling, sampling, [], [], shape, dtype, format)
            
            # If present, read meta:
            try:
                meta = io.read_meta(path, sample = sampling)
                
            except:
                logger.warning('No meta data found.')
                meta = {}
                
            # Remember the path to the data using meta:    
            meta['path'] = path  
            
            self.set_output(meta = meta, index = ii)
                        
    def runtime(self):
        # Get arguments:
        paths, name, sampling, shape, dtype, format, flipdim, proj_number = self.arguments
        paths = io.get_folders_sorted(paths)
           
        # Read!
        for ii, path in enumerate(paths):
       
          logger.print('Reading data @ ' + path)
          data = io.read_stack(path, name, sampling, sampling, shape = shape, dtype = dtype, format = format, flipdim = flipdim)
          #proj, meta = process.process_flex(path, sampling, sampling, proj_number = proj_number) 
            
          self.set_output(data = data, index = ii)
   
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> SCHEDULER CLASS >>>>>>>>>>>>>>>>>>>>>>>>>>>>
          
class scheduler:
   '''
   Class responsible for scheduling tasks by creating tree of processing nodes connected via buffers.
   Scheduler links to the root_node that provides an entry point to the node tree.
   
   ''' 
   
   def __init__(self, _scratch_path_, clean_scratch = True):
   
      self.root_node = None      
      
      if not _scratch_path_:
          logger.error('Scratch path is missing!')
      
      self._scratch_path_ = _scratch_path_
      
      if clean_scratch:            
          self._clean_scratch_dir_()
          
   def draw_nodes(self):
       """
       Draw the node tree.
       """
        
       G = self._get_nodesgraph_()
       
       # Compute nodes positions:
       pos=networkx.drawing.nx_agraph.graphviz_layout(G, prog='dot')
           
       plt.figure(figsize=(7,10)) 
       plt.title('Node Tree', fontsize=15, fontweight = 'bold')
       
       # Draw edges:
       edge_color = [G.edges[key]['edgecolor'] for key in G.edges.keys()]
       
       networkx.draw_networkx_edges(G, pos, edge_color = edge_color, width = 4, node_size = 500, alpha=0.3)
              
       # Draw nodes:       
       node_color = [G.nodes[key]['fillcolor'] for key in G.nodes.keys()]
       
       networkx.draw_networkx_nodes(G, pos, node_color = node_color, node_size = 500, alpha=0.3)
       networkx.draw_networkx_labels(G, pos, node_size = 500, with_labels=True, font_size = 12, font_weight = 'bold')
       
       plt.axis('off')
       plt.show()
               
   def _clean_scratch_dir_(self):
      """
      Remove all scratch files in a directory.
      """
      if os.path.exists(self._scratch_path_):
            
        # Find old scratch files:
        files = os.listdir(self._scratch_path_)
        
        for file in files:
            file_ = os.path.join(self._scratch_path_, file)
            
            if os.path.isfile(file_): 
                
                logger.print('Removing scratch file @ ' + file_)
                os.remove(file_)
   
   def _free_buffers_(self):
      """
      Get buffers that are at the end of the node tree.
      """
      # Get nodes at the bottom of the tree:   
      nodes = self._get_free_nodes_(self.root_node)
      
      # Get the buffers at the bottom:
      buffers = []
      for node in nodes:
         buffers.extend(node.outputs)
         
      return buffers 
  
   def _get_free_nodes_(self, node):
      """
      Recursively go down the node tree and return the nodes at the bottom.
      """
      # Nodes below the current one:
      nodes = node.get_children()
      
      if nodes == []:
         return [node,]
         
      else:
         subnodes = []
         
         for node_ in nodes:
            free_nodes = self._get_free_nodes_(node_)
            
            # Check uniqness:
            for node in free_nodes:
                if node not in subnodes: 
                    subnodes.append(node)
            
         return subnodes
  
   def _count_nodes_(self, nodes, state):
      """
      Count how many nodes of a particular state there are.
      """
      count = 0
      
      for node in nodes:
          if node.state == state: count += 1
      
      return count
  
   def _state2color_(self, state):
       """
       Small routine converting states to colors for drawing the node tree.
       """
       if state == _NSTATE_PENDING_:
           return 'red'
       
       elif state == _NSTATE_ACTIVE_:
           return 'yellow'
       
       elif state == _NSTATE_DEACTIVATED_:
           return 'green'
       
   def _get_nodesgraph_(self):
      """
      Returns networkx.MultiDiGraph object for the given node.
      """ 
      # Directional multi-graph:
      G = networkx.Graph()
      
      level_no = 0
      paren_level = [self.root_node,]
      
      name = ('[%u.%u]' % (0, 0)) + self.root_node.node_name
      G.add_node(name, fillcolor = self._state2color_(self.root_node.state))      
      
      #G.add_node(old_name)
      
      while paren_level:
                    
          # Loop over nodes in old level:
          sub_no = 0
          level = []
          for ii, node in enumerate(paren_level):
              
              parent_name = ('[%u.%u]' % (level_no, ii)) + node.node_name
              
              # Make a new level making sure every node is unique:
              children = node.get_children()
              
              for child in children:
                  
                  # Add a unique link:
                  if child not in level:
                      level.append(child)
                      sub_no += 1
                    
                  child_name = ('[%u.%u]' % (level_no+1, sub_no - 1)) + child.node_name    
                  
                  G.add_node(child_name, fillcolor = self._state2color_(child.state))
                  G.add_edge(parent_name, child_name, edgecolor = self._state2color_(child.state))
                  
          paren_level = level   
          level_no += 1    
              
      return G
  
   def _get_nodes_(self, node, state = None):
      """
      Returns nodes with the given status.
      """
      
      if (state is None) or (node.state == state):
          out = [node,]
          
      else:
          out = []
      
      children = node.get_children()
      
      for child in children:
          
          # get descendent nodes but only unique ones:
          nodes = self._get_nodes_(child, state)
          
          for node in nodes:
              if node not in out: 
                  out.append(node)
                     
      return out
  
   def _get_nodeready_(self, node):
      """
      Returns a single node that is ready for activation.
      """
      if node is None: return None
      
      if node.state == _NSTATE_PENDING_:
          if node.isready():
              return node
          
          else:  
              # Node may be PENDING but waiting for a group output - in that case need to switch to another branch.
              return None

      # Return which ever is ready node:          
      nodes = node.get_children()      
      for node in nodes:
          
          out = self._get_nodeready_(node)
          
          if out is not None:
              return out
          
      return None      
                    
   def backup(self):
      """
      Save the node tree on disk.
      """
      logger.print('Backing up the tree of nodes.')
      
      nodes = self._get_nodes_(self.root_node)
      
      # Count pending nodes:
      pend = self._count_nodes_(nodes, _NSTATE_PENDING_)
      deactive = self._count_nodes_(nodes, _NSTATE_DEACTIVATED_)
      active = self._count_nodes_(nodes, _NSTATE_ACTIVE_)
     
      logger.print('%u pending | %u active | %u deactivated' % (pend, active, deactive))
      
      file = os.path.join(self._scratch_path_, 'nodes.pickle')
      pickle_out = open(file, "wb")
      pickle.dump(nodes, pickle_out)
      pickle_out.close()
      
   def restore_nodes(self):
      """
      Load the node tree from disk.
      """
      logger.print('Loading nodes tree.')
      
      file = os.path.join(self._scratch_path_, 'nodes.pickle')
      
      pickle_in = open(file,"rb")
      nodes = pickle.load(pickle_in)        
      
      self.root_node = nodes[0]
              
   def schedule(self, node_class, arguments = []):
      """
      Schedule nodes. 
      """
      
      # Create the first node if needed:
      if self.root_node is None:
         self.root_node = node_class(self, arguments, [])
         
      else:
         # Get free buffers: 
         buffers = self._free_buffers_()
      
         # Create one node per buffer or one node for all buffers:
         if node_class.node_type == _NTYPE_BATCH_:
         
            for buffer in buffers:
               # Create an instance of a node:
               buffer.reader_node = node_class(self, arguments, [buffer,])
               
         elif node_class.node_type == _NTYPE_GROUP_:   
             
            node = node_class(self, arguments, buffers)
            
            for buffer in buffers:
                # Link all buffers with the same node:
                buffer.reader_node = node
            
         else:
            logger.error('Unknown node type:' + str(node_class.node_type))
   
   def schedule_batch(self, callback, **arguments):
       """
       Schedule a standard batch node with one input and one output using the given callback function.
       """
       # Pass the callback as the first argument for batch_node:
       self.schedule(batch_node, (callback, arguments))

   def FDK(self, vol_shape = None):
       
      arguments = (vol_shape, )
      self.schedule(fdk_node, arguments)   

   def soft_threshold(self, mode, threshold = None):
      """
      Removes values smaller than the threshold value.
      
      Args:
        mode (str)       : 'histogram', 'otsu' or 'constant'
        threshold (float): threshold value if mode = 'constant'
      """
      arguments = (mode, threshold)
      
      self.schedule(threshold_node, arguments)   
      
   def beamhardening(self, file, compound, density):
      """
      Single material beamhardening based on a file with an effective spectrum record.
        Args:            
            file    : filepath of the spectrum record
            compound: chemical formula of the specimen material
            density : density in g / cm3
      """
      arguments = (file, compound, density)
      
      self.schedule(beamhardening_node, arguments)  
      
   def markernorm(self, norm, size = 5):
      """
      Find a marker and normalize density of that marker to match the given value
        Args:            
            norm : value used for normalization
            size : size of the marker (diametre in mm)
      """
      arguments = (norm, size)
      
      self.schedule(markernorm_node, arguments)   
      
   def pad(self, width, dim, mode = 'linear'):
      """
      Schedule padding operation.
      """
      arguments = (width, dim, mode)
      
      self.schedule(pad_node, arguments) 

   def bin(self, dim):
      """
      Schedule a bin operation.
      """
      arguments = (dim,)
      self.schedule(bin_node, arguments) 
      
   def crop(self, dim, width):
      """
      Schedule a crop operation.           
      """
      arguments = (dim, width)
      self.schedule(crop_node, arguments)  
      
   def autocrop(self):
      """
      Schedule autocrop operation.            
      """      
      self.schedule(autocrop_node)   


   def cast2type(self, dtype, bounds = None):
      """
      Schedule a cast to type operation. 
        Args:            
            
      """
      arguments = (dtype, bounds)
      
      self.schedule(cast2type_node, arguments)   

   def buffer_info(self):
       """
       Print data and meta info.
       """
       self.schedule(info_node)
           
   def read_data(self, paths, name, sampling = 1, shape = None, dtype = None, format = None, flipdim = True, proj_number = None):
      """
      Schedule an image stack reader. Often will be the first node in the queue.
        Args:
            
      """
      arguments = (paths, name, sampling, shape, dtype, format, flipdim, proj_number)
      self.schedule(reader_node, arguments)
      
   def write_data(self, path, name, dim = 0, skip = 1, compress = True):
      """
      Schedule an image stack writer. 
        Args:
            
      """
      arguments = (path, name, dim, skip, compress)
      
      self.schedule(writer_node, arguments)      
   
   def display(self, display_type, **argin):

       self.schedule(display_node, [display_type, argin])      
   
   def FDK(self, vol_shape = None):
       
      arguments = (vol_shape, )
      self.schedule(fdk_node, arguments)      
   
   def cast2type(self, dtype, bounds = None):
      """
      Schedule a cast to type operation. 
        Args:            
            
      """
      arguments = (dtype, bounds)
      
      self.schedule(cast2type_node, arguments)       

   def merge(self, mode = 'projections'):
      """
      Schedule a data merge operation. 
        Args:
            mode(str): use 'projections' or 'volume', depending on the type of the input.
      """
      if mode == 'projections':
          self.schedule(proj_merge_node)       
          
      elif mode == 'volume':
          self.schedule(vol_merge_node)       
          
      else:
          logger.error('Unknown mode!')
      
   def flatlog(self, usemax = False, flats = '', darks = '', sample = 1, flipdim = False):
       """
       Read flats and darks and apply them to projection data or use 'usemax' mode to perform a data-driven correction.
       """
       arguments = (usemax, flats, darks, sample, flipdim)
       self.schedule(flatlog_node, arguments)
   
   def optimize(self, values, key = 'axs_hrz', tile_index = None, sampling = [10, 1, 1], metric = 'correlation'):
       """
       Optimize a parameter using parameter range, geometry key, tile number and sub-sampling.
       """
       arguments = (values, key, tile_index, sampling, metric)
       self.schedule(optimize_node, arguments) 
    
   def report(self):
      """
      Print the node tree.
      """
      
      time.sleep(0.3)
      
      logger.title('Reporting nodes:')
     
      nodes = self._get_nodes_(self.root_node, state = None)
      
      for node in nodes:
          print(node.node_name + ' : ' + node.state2str())
          
   def cleanup(self): 
      """
      Remove files after a succesfull run.
      """
      
      logger.title('Cleaning up.')
      
      gc.collect()
      
      # Find ready nodes:
      nodes = self._get_nodes_(self.root_node, _NSTATE_DEACTIVATED_)
      
      # Cleanup nodes:
      for node in nodes:
          node.cleanup()
      
   def run(self):
      """
      Run scheduled nodes.
      """
      logger.title('*** Runtime ***')
      
      # Save a checkpoint:
      self.backup()
      
      # Find a pending node:
      node = self._get_nodeready_(self.root_node)
          
      # Run nodes until they are finished:
      while node:    
                    
          # Activate next node:
          logger.print('____________________________________')
          node.activate()
          
          # Save a checkpoint:
          self.backup()
          
          # Next node ready:
          node = self._get_nodeready_(self.root_node)
          
      logger.title('*** End Runtime ***')