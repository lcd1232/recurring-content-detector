import os
import itertools
import operator
import numpy as np
import pickle
import faiss
import datetime

# internal imports
import config
import featurevectors
import video_functions
import evaluation

def max_two_values(d):
    """ 
    a) create a list of the dict's keys and values; 
    b) return the two keys with the max values
    """  
    v=list(d.values())
    k=list(d.keys())
    result1 = k[v.index(max(v))]
    del d[result1]

    v=list(d.values())
    k=list(d.keys())
    result2 = k[v.index(max(v))]
    return [result1, result2]


def fill_gaps(sequence, lookahead):
    """
    Given a list consisting of 0's and 1's , fills up the gaps between 1's 
    if the gap is smaller than the lookahead.

    Example: 
        input: [0,0,1,0,0,0,0,1,0,0] with lookahead=6
       output: [0,0,1,1,1,1,1,1,0,0]
    """
    i = 0
    while i < len(sequence) - lookahead:
        current = sequence[i]
        next = sequence[i + 1 : i + lookahead].tolist()
        
        if current and True in next:
            x = 0
            while not next[x]:
                sequence[i + 1 + x] = True
                x = x + 1
                
        i = i + 1

    return sequence

def get_two_longest_timestamps(timestamps):
    """
    Returns the two longest time intervals given a list of time intervals

    Example: 
        input: [(0,10) , (0,5) , (20,21)]
        returns: [(0,10), (0,5)]
    """
    # if size is smaller or equal to 2, return immediately
    if len(timestamps) <= 2:
        return timestamps

    d = {}
    for start,end in timestamps:
        d[(start,end)] = end - start

    return max_two_values(d)

def to_time_string(seconds):
    """
    Given seconds in integer format, returns a string in the format hh:mm:ss (example: 01:30:45)
    """
    return str(datetime.timedelta(seconds=seconds))


def detect(video_dir, annotations = None):
    # the video files used for the detection
    videos = [f for f in os.listdir(video_dir) if os.path.isfile(os.path.join(video_dir, f))]
    # location of the vector directory
    vectors_dir = os.path.join(video_dir, "resized", "feature_vectors_framejump{}".format(config.FRAMEJUMP))

    # if there's an annotations file, get the pandas format
    if annotations is not None:
        annotations = evaluation.get_annotations(annotations)

    for file in videos:
        # set the video path files
        file_full = os.path.join(video_dir, file)
        file_resized = os.path.join(video_dir, "resized", file)

        # make sure folder of experimentname exists or create otherwise
        os.makedirs(os.path.dirname(file_resized), exist_ok=True)
        
        # if there is no resized video yet, then resize it
        if not os.path.isfile(file_resized):
            video_functions.resize(file_full, file_resized)

        # from the resized video, construct feature vectors
        featurevectors.construct_feature_vectors(   
            file_resized, "feature_vectors_framejump{}".format(config.FRAMEJUMP), featurevectors.color_hist)


    vector_files = [os.path.join(vectors_dir,e+'.p') for e in videos]
    vectors = []

    # the lengths of each vector, will be used to query each episode
    lengths = []

    # concatenate all the vectors into a single list multidimensional array
    for f in vector_files:
        h = np.array(pickle.load(open(f, "rb")), np.float32)
        lengths.append(h.shape[0])
        vectors.append(h)

    vectors = np.vstack(vectors)

    results = []
    for i, length in enumerate(lengths):
        i += 1
        s = sum(lengths[:i-1])
        e = sum(lengths[:i])

        # query consists of one episode
        query = vectors[s:e]
        # rest of the feature vectors
        rest = np.append(vectors[:s], vectors[e:], axis=0)

        # build the faiss index, set vector size
        vector_size = query.shape[1]
        index = faiss.IndexFlatL2(vector_size)    
        # add vectors of the rest of the episodes to the index
        index.add(rest)

        # we want to see k nearest neighbors
        k = 1
        # search with for matches with query
        scores, indexes = index.search(query, k)
        
        result = scores[:,0]    
        results.append((videos[i-1], result))
    
    # evaluation variables
    total_relevant_seconds = 0
    total_detected_seconds = 0
    total_relevant_detected_seconds = 0

    framejump = config.FRAMEJUMP

    all_detections = {}
    for video, result in results:
        framerate = video_functions.get_framerate(os.path.join(video_dir, video))
        threshold = np.percentile(result, config.PERCENTILE)

        # all the detections
        below_threshold = result < threshold
        # Merge all detections that are less than 10 seconds apart
        below_threshold = fill_gaps(below_threshold, int((framerate/config.FRAMEJUMP) * 10))

        # put all the indices where values are nonzero in a list of lists
        nonzeros = [[i for i, value in it] for key, it in itertools.groupby(
            enumerate(below_threshold), key=operator.itemgetter(1)) if key != 0]

        detected_beginning = []
        detected_end = []

        for nonzero in nonzeros:
            start = nonzero[0]
            end = nonzero[-1]

            #result is in first 20% of the video
            occurs_at_beginning = end < len(result) / 5
            #the end of this timestamp ends in the last 15 seconds             
            ends_at_the_end = end > len(result) - 15 * (framerate/framejump) 

            if (end - start > (15 * (framerate / framejump)) #only count detection when larger than 15 seconds             
                and (occurs_at_beginning or ends_at_the_end)): #only use results that are in first 1/5 part or end at last 15 s            

                start = start / (framerate / framejump)
                end = end / (framerate / framejump)

                if occurs_at_beginning:
                    detected_beginning.append((start,end))
                elif ends_at_the_end:
                    detected_end.append((start,end))


        detected = get_two_longest_timestamps(detected_beginning) + detected_end

        print("Detections for: {}".format(video))
        for start,end in detected:
            
            print("{} \t \t - \t \t {}".format(to_time_string(start), to_time_string(end)))
        print()

        # evaluation
        if annotations is not None:
            ground_truths = evaluation.get_skippable_timestamps_by_filename(video, annotations)
            relevant_seconds, detected_seconds, relevant_detected_seconds = evaluation.match_detections_precision_recall(
                detected, ground_truths)

            total_relevant_seconds += relevant_seconds
            total_detected_seconds += detected_seconds
            total_relevant_detected_seconds += relevant_detected_seconds

        all_detections[video] = detected

    if annotations is not None:
        precision = total_relevant_detected_seconds / total_detected_seconds
        recall = total_relevant_detected_seconds / total_relevant_seconds

        print("Total precision = {0:.3f}".format(precision))
        print("Total recall = {0:.3f}".format(recall))

    
    return all_detections

print(detect("./test", "annotations.csv"))