import os
import re
import itertools

import numpy as np
import pandas as pd

from .config import ISOCHRONES
from .interp import DFInterpolator
from .mags import interp_mag, interp_mags
from .grid import Grid


class ModelGrid(Grid):

    default_columns = ('eep', 'age', 'feh', 'mass', 'initial_mass', 'radius',
                       'logTeff', 'Teff', 'logg', 'logL', 'Mbol')


    @property
    def prop_map(self):
        return dict(eep=self.eep_col, age=self.age_col, feh=self.feh_col,
                    mass=self.mass_col, initial_mass=self.initial_mass_col,
                    logTeff=self.logTeff_col, logg=self.logg_col, logL=self.logL_col)

    @property
    def column_map(self):
        return {v: k for k, v in self.prop_map.items()}

    @property
    def datadir(self):
        return os.path.join(ISOCHRONES, self.name)

    @property
    def kwarg_tag(self):
        raise NotImplementedError

    def get_directory_path(self, **kwargs):
        raise NotImplementedError

    def get_existing_filenames(self, **kwargs):
        d = self.get_directory_path(**kwargs)
        if not os.path.exists(d):
            self.extract_tarball(**kwargs)
        return [os.path.join(d, f) for f in os.listdir(d) if re.search(self.filename_pattern, f)]

    def get_filenames(self, **kwargs):
        """ Returns list of all filenames corresponding to phot system and kwargs.
        """
        return self.get_existing_filenames(**kwargs)

    @classmethod
    def get_feh(cls, filename):
        raise NotImplementedError

    @classmethod
    def to_df(cls, filename):
        """Parse raw filename to dataframe
        """
        raise NotImplementedError

    def df_all(self):
        """Entire original model grid as dataframe

        TODO: also save this as HDF, in case it's useful for anything
        """
        df = pd.concat([self.to_df(f) for f in self.get_filenames()])
        df = df.sort_values(by=list(self.index_cols))
        df.index = [df[c] for c in self.index_cols]
        return df

    def compute_additional_columns(self, df):
        """
        """
        df['Teff'] = 10**df['logTeff']
        df['Mbol'] = 4.74 - 2.5 * df['logL']
        df['radius'] = 10**df['log_R']
        return df

    def get_df(self):
        """Returns column-mapped, pared-down, standardized version of model grid
        """
        df = self.df_all()
        df = df.rename(columns=self.column_map)
        df = self.compute_additional_columns(df)
        # Select only the columns we want
        df = df[list(self.default_columns)]
        return df

    @property
    def hdf_filename(self):
        return os.path.join(self.datadir, '{}{}.h5'.format(self.name, self.kwarg_tag))

    def get_dm_deep(self, compute=False):
        filename = os.path.join(self.datadir, 'dm_deep{}.h5'.format(self.kwarg_tag))

        compute = not os.path.exists(filename)

        if not compute:
            try:
                dm_deep = pd.read_hdf(filename, 'dm_deep')
            except Exception:
                compute = True

        if compute:
            # need grid to work with first
            df = self.get_df()

            # Make bucket for derivative to go in
            df['dm_deep'] = np.nan

            # Compute derivative for each (feh, age) isochrone, and fill in
            for f, a in itertools.product(*df.index.levels[:2]):
                subdf = df.loc[f, a]
                deriv = np.gradient(subdf['initial_mass'], subdf['eep'])
                subdf.loc[:, 'dm_deep'] = deriv

            df.dm_deep.to_hdf(filename, 'dm_deep')
            dm_deep = pd.read_hdf(filename, 'dm_deep')

        return dm_deep

    @property
    def df(self):
        if self._df is None:
            self._df = self.read_hdf()
            self._df['dm_deep'] = self.get_dm_deep()

        return self._df

    @property
    def interp_grid_npz_filename(self):
        return os.path.join(self.datadir, 'full_grid{}.npz'.format(self.kwarg_tag))

    @property
    def interp(self):
        if self._interp is None:
            self._interp = DFInterpolator(self.df, filename=self.interp_grid_npz_filename)
        return self._interp


class ModelGridInterpolator(object):

    grid_type = None
    bc_type = None

    # transformation from desired param order to that expected by interp functions
    _param_index_order = (1, 2, 0, 3, 4)

    def __init__(self, bands=None):
        self.bands = bands if bands is not None else list(self.bc_type.default_bands)

        self._model_grid = None
        self._bc_grid = None

        self.param_index_order = list(self._param_index_order)

    @property
    def model_grid(self):
        if self._model_grid is None:
            self._model_grid = self.grid_type()
        return self._model_grid

    @property
    def bc_grid(self):
        if self._bc_grid is None:
            self._bc_grid = self.bc_type(self.bands)
        return self._bc_grid

    def interp_value(self, pars, props):
        """

        pars : age, feh, eep, [distance, AV]
        """
        try:
            pars = np.atleast_1d(pars[self.param_index_order])
        except TypeError:
            i0, i1, i2, i3, i4 = self.param_index_order
            pars = [pars[i0], pars[i1], pars[i2]]
        return self.model_grid.interp(pars, props)

    def interp_mag(self, pars, bands):
        """

        pars : age, feh, eep, distance, AV
        """
        i_bands = [self.bc_grid.interp.columns.index(b) for b in bands]

        try:
            pars = np.atleast_1d(pars).astype(float)

            return interp_mag(pars, self.param_index_order,
                              self.model_grid.interp.grid,
                              self.model_grid.interp.column_index['Teff'],
                              self.model_grid.interp.column_index['logg'],
                              self.model_grid.interp.column_index['feh'],
                              self.model_grid.interp.column_index['Mbol'],
                              *self.model_grid.interp.index_columns,
                              self.bc_grid.interp.grid, i_bands,
                              *self.bc_grid.interp.index_columns)
        except (TypeError, ValueError):
            # Broadcast appropriately.
            b = np.broadcast(*pars)
            pars = np.array([np.resize(x, b.shape).astype(float) for x in pars])
            return interp_mags(pars, self.param_index_order,
                               self.model_grid.interp.grid,
                               self.model_grid.interp.column_index['Teff'],
                               self.model_grid.interp.column_index['logg'],
                               self.model_grid.interp.column_index['feh'],
                               self.model_grid.interp.column_index['Mbol'],
                               *self.model_grid.interp.index_columns,
                               self.bc_grid.interp.grid, i_bands,
                               *self.bc_grid.interp.index_columns)


