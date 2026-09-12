import pandas as pd
import numpy as np
import pickle
import matplotlib.pyplot as plt
import seaborn as sns

dataset = pd.read_csv("Dataset/CrimesOnWomenData.csv")
states = np.unique(dataset['State'])
print(states)

crime = dataset.loc[dataset['State'] == 'ANDHRA PRADESH']
crime = crime['Rape'].ravel()[0]
print(crime)
'''
f = open('model/model_history.pckl', 'rb')
hist = pickle.load(f)
f.close()

print(hist)

plt.figure(figsize=(5,3))
plt.plot(hist['accuracy'], color = 'blue', label = 'Training Accuracy')
plt.plot(hist['val_accuracy'], color = 'green', label = 'Validation Accuracy')
plt.title('Neural Network Accuracy Graph')
plt.xlabel('Epochs')
plt.ylabel('Accuracy')
plt.legend()
plt.show()    
'''
heatmap = dataset.groupby('State')['Rape'].sum().sort_values(ascending=False).reset_index(name="Total Crimes")[0:10]
print(heatmap)
labels = heatmap['State'].ravel()
heatmap.drop(['State'], axis = 1,inplace=True)
heatmap = heatmap.values
data = []
for i in range(len(heatmap)):
    values = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    values[i] = heatmap[i,0]
    data.append(values)
heatmap = np.asarray(data)    
ax = sns.heatmap(heatmap, xticklabels = labels, yticklabels = labels, annot = True, cmap="viridis" ,fmt ="g");
ax.set_ylim([0,len(heatmap)])
plt.title("Women Incident Crime Heatmap") 
plt.show()    
