from .reader import read_source
FIELDS=['id', 'kind', 'device', 'at', 'power', 'unit', 'irradiance', 'status', 'start', 'end']
def apply(state, provisional=False):read_source(state,FIELDS,{'编号':'id'},provisional=provisional)
