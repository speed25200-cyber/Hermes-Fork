import numpy as np, pandas as pd
Z = np.load('/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/xvenue/data/panel.npz', allow_pickle=True)
coins = [str(c) for c in Z['coins']]; time = pd.to_datetime(Z['time'].astype('int64'), unit='ns', utc=True)
j = coins.index('AXS')
df = pd.DataFrame({'fb': Z['fb'][:, j], 'fo': Z['fo'][:, j], 'bc': Z['bc'][:, j], 'oc': Z['oc'][:, j]}, index=time)
m = df.groupby(df.index.to_period('M')).agg(fb_ann=('fb', lambda x: x.sum()), fo_ann=('fo', lambda x: x.sum()), nb=('fb', lambda x: (x != 0).sum()), no=('fo', lambda x: (x != 0).sum()),
                                                   fbmin=('fb','min'), fomin=('fo','min'), fbmax=('fb','max'), fomax=('fo','max'))
hours = df.groupby(df.index.to_period('M')).size()
m['fb_ann'] *= 8760 / hours; m['fo_ann'] *= 8760 / hours; m['diff'] = m.fo_ann - m.fb_ann
pd.set_option('display.width', 250)
print(m['2024-06':].round(4).to_string())
